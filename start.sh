#!/bin/sh
gunicorn -w 2 -b 0.0.0.0:5000 login:app &
gunicorn -w 2 -b 0.0.0.0:5001 campaign:app &
gunicorn -w 2 -b 0.0.0.0:5002 youtube.login:app &
gunicorn -w 2 -b 0.0.0.0:5003 X.login:app &
gunicorn -w 2 -b 0.0.0.0:5004 Whatsapp.login:app &
gunicorn -w 2 -b 0.0.0.0:5005 threads.login:app &
gunicorn -w 2 -b 0.0.0.0:5006 threads.webhook:app &
gunicorn -w 2 -b 0.0.0.0:5007 Razorpay.payment:app &
gunicorn -w 2 -b 0.0.0.0:5008 pinterst.login:app &
gunicorn -w 2 -b 0.0.0.0:5009 Paypal.paypal_payments:app &
gunicorn -w 2 -b 0.0.0.0:5010 Instagram.Login:app &
gunicorn -w 2 -b 0.0.0.0:5011 Instagram.webhook:app &
gunicorn -w 2 -b 0.0.0.0:5012 Gmail.APP2:app &
gunicorn -w 2 -b 0.0.0.0:5013 Drive.dep:app &
gunicorn -w 2 -b 0.0.0.0:5014 Gmail.webhook:app &
wait
python3.10 -m limit &
python3.10 -m Instagram.schedule_video 