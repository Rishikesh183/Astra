# Hugging Face Space (Docker SDK) for Physics Witness: free CPU hardware, no card needed.
# Like deploy/nebius/start.sh, the container downloads the repo from GitHub on every start,
# installs it and serves; claims, videos and the model cache live in Supabase.
FROM python:3.11-slim

# Spaces run the container as uid 1000, so everything lives under that user's home.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PORT=7860 \
    PW_APP_DIR=/home/user/app \
    PW_DATA_DIR=/home/user/data
WORKDIR /home/user

EXPOSE 7860
CMD python -c "import os, urllib.request as u; ref = os.environ.get('PW_GIT_REF', 'develop'); u.urlretrieve(f'https://raw.githubusercontent.com/Rishikesh183/Astra/{ref}/deploy/nebius/start.sh', '/home/user/start.sh')" \
    && sh /home/user/start.sh
