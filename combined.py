from werkzeug.middleware.dispatcher import DispatcherMiddleware
from login import app as login
from campaign import app as campaign
from Instagram.Login import app as instagram
from youtube.login import app as youtube
from X.login import app as x
from Whatsapp.login import app as whatsapp
from threads.login import app as threads
from Razorpay.payment import app as razorpay
from pinterst.login import app as pinterest
from Paypal.paypal_payments import app as paypal
from Gmail.APP2 import app as gmail
from Drive.dep import app as drive
from threads.webhook import app as threads_webhook
from Instagram.webhook import app as instagram_webhook
from Gmail.webhook import app as gmail_webhook
from Whatsapp.webhook import app as whatsapp_webhook

application = DispatcherMiddleware(login, {
    "/campaign": campaign,
    "/instagram": instagram,
    "/youtube": youtube,
    "/x": x,
    "/whatsapp": whatsapp,
    "/threads": threads,
    "/razorpay": razorpay,
    "/pinterest": pinterest,
    "/paypal": paypal,
    "/gmail": gmail,
    "/drive": drive,
    "/threads-webhook": threads_webhook,
    "/instagram-webhook": instagram_webhook,
    "/gmail-webhook": gmail_webhook,
    "/whatsapp-webhook": whatsapp_webhook,
})