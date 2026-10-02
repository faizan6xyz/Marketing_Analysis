# ARG PYTHON_VERSION=3.10
# ARG APP_VERSION=1.0
# FROM docker.io/library/python:${PYTHON_VERSION}-slim
# WORKDIR /app
# LABEL maintainer="faizan"
# LABEL version="${APP_VERSION}"
# RUN useradd -m appuser
# COPY requirement.txt .
# RUN python -m pip install --no-cache-dir -r requirement.txt
# COPY . .
# ENV PYTHONDONTWRITEBYTECODE=1
# ENV PYTHONUNBUFFERED=1
# EXPOSE 5000/tcp
# STOPSIGNAL SIGTERM
# ONBUILD COPY . .
# USER appuser
# CMD ["/start.sh"] 



ARG PYTHON_VERSION=3.10
ARG APP_VERSION=1.0
FROM docker.io/library/python:${PYTHON_VERSION}-slim
WORKDIR /app
LABEL maintainer="faizan"
LABEL version="${APP_VERSION}"
RUN useradd -m appuser
COPY requirement.txt .
RUN python -m pip install --no-cache-dir -r requirement.txt
COPY . .
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
EXPOSE 5000/tcp
STOPSIGNAL SIGTERM
USER appuser
RUN chmod 764 /app/start.sh
CMD ["./start.sh"]