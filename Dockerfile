FROM python:3.14-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY newsprism.py .
COPY static/ static/
COPY templates/ templates/

# Burn in the version (release tag or Git hash) at build time, since the
# container has no Git/.git at runtime. Filled at build time, e.g.
#   docker compose build --build-arg VERSION="$(git describe --tags --always --dirty)"
# (see docker-compose.example.yml). Falls back to "dev".
ARG VERSION=dev
ENV NEWSPRISM_VERSION=${VERSION}
ARG BUILD_DATE=
ENV NEWSPRISM_BUILD_DATE=${BUILD_DATE}

# Non-root user. UID/GID 1000 is the common default for the first user on
# most Linux systems - if the host user for the volumes (e.g. /data, /cache)
# has a different UID/GID, adjust at build time with
#   docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g)
# so the mounted directories stay writable.
ARG UID=1000
ARG GID=1000
RUN groupadd -g "${GID}" newsprism \
 && useradd -u "${UID}" -g "${GID}" -m -s /usr/sbin/nologin newsprism
USER newsprism

# In case a library wants to write under $HOME (e.g. cache fallbacks) -
# /home/newsprism is not writable under a read_only root, but /tmp via
# tmpfs is (see docker-compose.yml).
ENV HOME=/tmp

# CONFIG points to the mounted config.yaml; RUN_ONCE=1 for a one-off run.
ENV CONFIG=/config/config.yaml
CMD ["python", "-u", "newsprism.py"]
