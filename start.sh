#!/bin/sh
# All services pinned to CPU core 0, 1 worker each, several threads per worker.
# gthread = threads share one process's memory, so no extra worker copies.
CORE=0          # which CPU core to pin everything to
THREADS=4       # threads per service (raise for busy services, e.g. webhooks)
run() { # $1 = port, $2 = module:app, $3 = optional thread override
  taskset -c "$CORE" gunicorn  -w 1 -k gthread --threads "${3:-$THREADS}" --worker-tmp-dir /dev/shm --max-requests 1000 --max-requests-jitter 100 --timeout 60 --graceful-timeout 20 -b 0.0.0.0:"$1" "$2" & }
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
# Background jobs (must start BEFORE `wait`, which blocks forever)
taskset -c "$CORE" python3.10 -m limit &
taskset -c "$CORE" python3.10 -m Instagram.schedule_video &