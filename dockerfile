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
RUN groupadd project_m
RUN useradd -m appuser
RUN usermod -aG project_m appuser
COPY requirement.txt .
RUN python -m pip install --no-cache-dir -r requirement.txt
COPY . .
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
EXPOSE 5000/tcp
STOPSIGNAL SIGTERM
RUN chown -R root:project_m /app
RUN chmod 774 /app/start.sh
USER appuser
ENTRYPOINT ["./start.sh"]