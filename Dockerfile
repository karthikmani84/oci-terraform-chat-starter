# Optional prebuilt-image path. Default OCI deployment uses inline source.
# This official Python image is the Linux AMD64 image used by the original demo.
FROM docker.io/library/python@sha256:061c190f3d5815b341a18dfe8a45771f8ba68c15a607ea56997fde3009ac4eec
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PORT=8080
WORKDIR /opt/oci-terraform-chat
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY deploy/terraform-release.json ./deploy/terraform-release.json
COPY scripts/install_terraform.py ./scripts/install_terraform.py
RUN python scripts/install_terraform.py --output /usr/local/bin/terraform
COPY app ./app
COPY LICENSE ./LICENSE
RUN mkdir -p /data/workspace && chmod 700 /data /data/workspace
EXPOSE 8080
CMD ["python", "-m", "app.server"]
