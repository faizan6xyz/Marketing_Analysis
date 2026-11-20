from werkzeug.middleware.dispatcher import DispatcherMiddleware
from login import app as login
from threads.webhook import app as threads_webhook
from Instagram.webhook import app as instagram_webhook
from Gmail.webhook import app as gmail_webhook
from Whatsapp.webhook import app as whatsapp_webhook

application = DispatcherMiddleware(login, {
    "/threads-webhook": threads_webhook,
    "/instagram-webhook": instagram_webhook,
    "/gmail-webhook": gmail_webhook,
    "/whatsapp-webhook": whatsapp_webhook,
})