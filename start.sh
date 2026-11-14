#!/bin/sh
export MALLOC_ARENA_MAX=2
gunicorn -w 1 -k gthread --threads 16 --worker-tmp-dir /dev/shm --max-requests 5000 --max-requests-jitter 500 --timeout 60 --graceful-timeout 20  -b 0.0.0.0:5000 combined:application &
python3.14 -m limit &
python3.14 -m Instagram.schedule_video &
wait