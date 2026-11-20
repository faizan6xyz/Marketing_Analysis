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
})