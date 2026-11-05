"""Deterministic app-only ZIP; no state, config, SDK keys or prior deployments."""
from __future__ import annotations
import argparse,hashlib,io,json,pathlib,zipfile
ROOT=pathlib.Path(__file__).resolve().parents[1]
def package(root=ROOT):
 root=pathlib.Path(root)
 paths=[p for p in (root/'app').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix in {'.py','.html','.css','.js'}]
 paths += [root/'requirements.txt',root/'LICENSE',root/'deploy/terraform-release.json',root/'scripts/install_terraform.py']
 buf=io.BytesIO();members=[]
 with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
  for path in sorted(paths,key=lambda p:p.relative_to(root).as_posix()):
   if path.is_symlink():raise ValueError('Symlink source entry is not allowed')
   name=path.relative_to(root).as_posix();raw=path.read_bytes()
   info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
   info.external_attr=0o100644<<16;z.writestr(info,raw,compresslevel=9)
   members.append({'name':name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
 raw=buf.getvalue()
 return raw,{'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'base64Bytes':4*((len(raw)+2)//3),'members':members}
def main():
 p=argparse.ArgumentParser();p.add_argument('--source-root',type=pathlib.Path,default=ROOT);p.add_argument('--output',type=pathlib.Path,default=ROOT/'build/app.zip');a=p.parse_args()
 raw,proof=package(a.source_root);a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_bytes(raw)
 proof['archive']=str(a.output);a.output.with_suffix('.json').write_text(json.dumps(proof,indent=2)+'\n')
 print(json.dumps({k:proof[k] for k in ['archive','sha256','bytes','base64Bytes']}))
if __name__=='__main__':main()
