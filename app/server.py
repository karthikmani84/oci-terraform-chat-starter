"""Single always-running HTTP/chat/Terraform process behind the existing Gateway."""
from __future__ import annotations
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from .adoption import AdoptionService
from .cloud import ReadTools, clients
from .config import load_config, validate_config
from .engine import Engine
from .model import Model
from .storage import Missing, ObjectStore
from .terraform import TerraformRunner, safe_diagnostic


STATIC = Path(__file__).parent / 'static'


def build(config):
    config = validate_config(config)
    sdk = clients(config)
    store = ObjectStore(sdk['storage'], config['namespace'], config['bucket'])
    runner = TerraformRunner(Path(config.get('workdir', '/data/workspace')), config)
    engine_ref = []
    def code(): return engine_ref[0].current['files'] if engine_ref else store.json('workspace/current.json', {'files': {}})['files']
    runner.init()
    tools = ReadTools(sdk, config['compartment_id'], code, runner.schema().get('resource_schemas', {}))
    engine = Engine(config, store, runner, AdoptionService(runner), Model(sdk['model'], config, tools), tools)
    engine_ref.append(engine)
    return engine


def handler(engine, config):
    secret = secrets.token_bytes(32)
    expected_host = config.get('public_host', '')

    class Handler(BaseHTTPRequestHandler):
        server_version = 'TerraformChat/1'

        def log_message(self, fmt, *args):
            # Log method/path/status only; no headers, query values or credentials.
            print(json.dumps({'method': self.command, 'path': urlsplit(self.path).path,
                              'message': str(args[1]) if len(args) > 1 else ''}), flush=True)

        def _user(self):
            if config.get('local_demo') is True and self.client_address[0] not in {'127.0.0.1', '::1'}:
                raise PermissionError('Local demo requests must originate from loopback.')
            value = self.headers.get('X-Codegen-Subject', '')
            if not value and config.get('local_demo') is True and self.client_address[0] in {'127.0.0.1', '::1'}:
                value = 'local-demo'
            if not value or len(value) > 512: raise PermissionError('Sign in through the demo Gateway.')
            owner = hashlib.sha256(value.encode()).hexdigest()
            allowed = config.get('admin_subject_sha256')
            if allowed and owner != allowed and not config.get('local_demo'):
                raise PermissionError('This infrastructure chat is available to the configured administrator account.')
            return owner

        def _csrf(self, user):
            return hmac.new(secret, user.encode(), hashlib.sha256).hexdigest()

        def _send(self, value, status=200):
            body = json.dumps(value, ensure_ascii=True).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def _error(self, exc):
            if isinstance(exc, Missing): self._send({'error': 'Conversation not found.'}, 404)
            elif isinstance(exc, PermissionError):
                reason = {
                    'Sign in through the demo Gateway.': 'SIGN_IN_REQUIRED',
                    'Refresh the page to renew this session.': 'CSRF_MISMATCH',
                    'Open this action from the chat page.': 'ORIGIN_MISMATCH',
                }.get(str(exc), 'ACCESS_REJECTED')
                print(json.dumps({'event': 'request_rejected', 'status': 403, 'reason': reason}), flush=True)
                self._send({'error': str(exc), 'code': reason}, 403)
            elif isinstance(exc, (ValueError, KeyError)): self._send({'error': safe_diagnostic(str(exc))}, 400)
            else:
                code = getattr(exc, 'code', 'UNEXPECTED_ERROR')
                print(json.dumps({'event': 'request_error', 'code': code,
                                  'status': getattr(exc, 'status', None),
                                  'request_id': getattr(exc, 'request_id', None)}), flush=True)
                self._send({'error': 'The service could not finish this request. Your conversation is saved.', 'code': code}, 500)

        def do_GET(self):
            try:
                path = urlsplit(self.path).path
                if path == '/health': return self._send({'ready': True})
                user = self._user()
                if path == '/api/config':
                    return self._send({'scope': {'name': config.get('scope_name', 'Managed compartment'), 'id': config['compartment_id'],
                                                'region': config['region']},
                                       'user': 'Signed-in administrator', 'status': 'ready',
                                       'csrf_token': self._csrf(user)})
                if path == '/api/conversations': return self._send({'conversations': engine.list(user)})
                if path.startswith('/api/conversations/'):
                    return self._send(engine.get(path.split('/')[3], user))
                files = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
                         '/style.css': ('style.css', 'text/css')}
                if path not in files: return self._send({'error': 'Not found'}, 404)
                filename, mime = files[path]
                body = (STATIC / filename).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', mime + '; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
                self.end_headers(); self.wfile.write(body)
            except Exception as exc: self._error(exc)

        def do_POST(self):
            try:
                user = self._user()
                if not hmac.compare_digest(self.headers.get('X-Chat-CSRF', ''), self._csrf(user)):
                    raise PermissionError('Refresh the page to renew this session.')
                origin = self.headers.get('Origin', '')
                if origin and expected_host and origin != 'https://' + expected_host and not config.get('local_demo'):
                    raise PermissionError('Open this action from the chat page.')
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('Send a JSON request.')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 25000: raise ValueError('This message is too large.')
                body = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(body, dict): raise ValueError('Send a JSON object.')
                path = urlsplit(self.path).path
                if path == '/api/conversations': return self._send(engine.create(user), 201)
                parts = path.split('/')
                if len(parts) != 5 or parts[1:3] != ['api', 'conversations']:
                    return self._send({'error': 'Not found'}, 404)
                return self._send(engine.start(parts[3], user, parts[4], body), 202)
            except Exception as exc: self._error(exc)

    return Handler


def main():
    os.umask(0o077)
    config = load_config()
    engine = build(config)
    server = ThreadingHTTPServer((config.get('bind', '0.0.0.0'), int(os.environ.get('PORT', '8080'))), handler(engine, config))
    print(json.dumps({'event': 'ready', 'scope': config['compartment_id'], 'region': config['region']}), flush=True)
    server.serve_forever()


if __name__ == '__main__': main()
