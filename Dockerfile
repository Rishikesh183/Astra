FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY physics_witness ./physics_witness
COPY scripts ./scripts
RUN pip install --no-cache-dir .

# ffmpeg ships inside the imageio-ffmpeg wheel, so no apt packages are needed.
ENV PW_CACHE_DIR=/data/cache
VOLUME ["/data"]
ENTRYPOINT ["physics-witness"]
CMD ["--help"]
