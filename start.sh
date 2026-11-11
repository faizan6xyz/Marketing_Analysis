#!/bin/sh
# All services pinned to CPU core 0, 1 worker each, several threads per worker.
# gthread = threads share one process's memory, so no extra worker copies.
CORE=1          # which CPU core to pin everything to
THREADS=4       # threads per service (raise for busy services, e.g. webhooks)
run() { # $1 = port, $2 = module:app, $3 = optional thread override
  taskset -c "$CORE" gunicorn  -w 2 -k gthread --threads "${3:-$THREADS}" --worker-tmp-dir /dev/shm --max-requests 1000 --max-requests-jitter 100 --timeout 60 --graceful-timeout 20 -b 0.0.0.0:"$1" "$2" & }
run 5000 login:app
run 5001 campaign:app
run 5002 youtube.login:app
run 5003 X.login:app
run 5004 Whatsapp.login:app
run 5005 threads.login:app
run 5006 threads.webhook:app 8
run 5007 Razorpay.payment:app
run 5008 pinterst.login:app
run 5009 Paypal.paypal_payments:app
run 5010 Instagram.Login:app
run 5011 Instagram.webhook:app 8
run 5012 Gmail.APP2:app
run 5013 Drive.dep:app
run 5014 Gmail.webhook:app 8
run 5015 Whatsapp.webhook:app 8
# 12 services with 4 threads each = 96 threads. (2 worker * 12 app * 4 threads)
# 4 services with 8 threads each = 64 threads. (2 worker * 4 app * 8 threads)
# in total theres 160 (96+64) threads but only 1 core is allowed  
wait # Keep the shell alive while background processes run of the gunicorn  
taskset -c "$CORE" python3.10 -m limit &
taskset -c "$CORE" python3.10 -m Instagram.schedule_video &