# Who's Home settings.
# Copy this file to config.py, change what you need, then deploy (or restart the service).

# The BT Smart Hub 2's address on your network
HUB_IP = "192.168.1.254"
HUB_MODEL = 2

# How often to ask the hub who's connected (seconds)
POLL_SECONDS = 60

# Phones nap on Wi-Fi to save battery. Only treat someone as "gone out"
# once their phone has been missing for this many minutes.
AWAY_AFTER_MINUTES = 25

# Keep this many days of history, older entries are tidied away nightly
HISTORY_DAYS = 30

# Devices not seen for this many days are tucked away under "Gone quiet"
QUIET_AFTER_DAYS = 60

# Unnamed devices not seen for this many days are forgotten automatically.
# Named ones are always kept. Set to 0 to switch this off.
FORGET_AFTER_DAYS = 180

# How often to ask devices their names (minutes). It also asks straight away
# whenever an unnamed device connects, and when you press the button.
DISCOVER_MINUTES = 60

# Web page port (pick one nothing else on the Pi is using)
WEB_PORT = 5050

# Phone notifications using the free ntfy app.
# Install ntfy on your phone, subscribe to the topic below, and you're done.
# Pick a long, hard-to-guess topic name, as anyone who knows it can read it.
# Leave NTFY_TOPIC empty to switch notifications off.
NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC = ""

# Notify when someone arrives home or goes out, as well as for new devices
NOTIFY_ARRIVALS = True

# Database file, sits next to the code
DB_PATH = "whoshome.db"
