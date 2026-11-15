"""Custom emojis of the bot (application emojis, copied from the Moddy design kit).

Rules (``docs/design/DESIGN.md`` §4): custom emojis only, never a Unicode emoji (country
flags excepted); always import a constant, never paste an id elsewhere; one icon, one
meaning; no "roughly right" icon: no icon rather than a misleading one.
Id mapping: ``docs/design/emoji_mapping.json``.
"""

from __future__ import annotations

# Status indicators
DONE = "<:done:1571617496765563030>"
UNDONE = "<:undone:1571617498946740249>"
ERROR = "<:error:1571617376393232516>"
WARNING = "<:warning:1571617372643532860>"
EXCLAMATION = "<:exclamation:1571617588394598401>"  # global sanction notices
INFO = "<:info:1571617417048621138>"
LOADING = "<a:spinner:1571617559063568484>"
ROBOT_WORKING = "<a:Robot:1571617562117275699>"

# Status dots
GREEN_STATUS = "<:green_status:1571617291156717600>"
YELLOW_STATUS = "<:yellow_status:1571617293560053760>"
RED_STATUS = "<:red_status:1571617295069876287>"

# Actions
ADD = "<:add:1571617388389077073>"
EDIT = "<:edit:1571617439551197386>"
DELETE = "<:delete:1571617436875100200>"
SAVE = "<:save:1571617394684596415>"
SEARCH = "<:search:1571617378184331355>"
SYNC = "<:sync:1571617522824908810>"
BACK = "<:back:1571617434924744764>"
NEXT = "<:next:1571617385998450872>"
PAUSE = "<:pause:1571617343174615251>"
PLAY = "<:play:1571617347704193144>"
LABS = "<:labs:1571618339061629058>"  # Simulation state (module state, automod dry run)
LOGOUT = "<:logout:1571617425135239268>"
DOWNLOAD = "<:download:1571617445523882014>"
REPLY = "<:reply:1571617374480891926>"

# Navigation / UI
SETTINGS = "<:settings:1571617494001520710>"
COMMANDS = "<:commands:1571617422446825684>"
TOGGLE_ON = "<:toogle_on:1571617996609429625>"
TOGGLE_OFF = "<:toogle_off:1571617998953914540>"
REQUIRED_FIELDS = "<:required_fields:1571617289151713482>"

# Objects / Concepts
USER = "<:user:1571617478734381146>"
DEV = "<:dev:1571617203797627100>"
BLACKLIST = "<:blacklist:1571617452016668742>"
TIME = "<:time:1571617463664382005>"
SNOWFLAKE = "<:snowflake:1571617456995434606>"
WEB = "<:web:1571617460808065054>"
HISTORY = "<:history:1571617447411327007>"
BOOK = "<:book:1571617358609645858>"
CODE = "<:code:1571617420693737694>"
BUG = "<:bug:1571617418810359988>"
BUILD = "<:build:1571618004557373564>"  # configuration help ("we set it up for you")
VERIFIED = "<:verified:1571617320856715344>"
VERIFIED_ORG = "<:verified_org:1571617322261553337>"
MINI_VERIFIED = "<:miniverified:1571617287314743518>"
NOTE = "<:note:1571617383322361866>"
MESSAGE = "<:message:1571617380050800880>"
SEND = "<:send:1571617411591831694>"  # "send a test message" buttons (/config)
GROUPS = "<:groups:1571617370034929746>"
WAVING_HAND = "<:waving_hand:1571617367920746710>"
FLAG = "<:flag:1571617366243025158>"
AT = "<:at:1571617363995131917>"
STAR = "<:star:1571617361507913748>"
EMOJI = "<:emoji:1571617504823091353>"
PREMIUM = "<:premium:1571617427073147011>"
SUPPORT = "<:support:1571617454646497415>"
BALANCE = "<:balance:1571617516776718500>"
MANAGE_USER = "<:manageuser:1571617476951941151>"
LEGAL = "<:legal:1571617509306536117>"
MIC_OFF = "<:mic_off:1571617533684089015>"
VOICE_CHAT = "<:voice_chat:1571617581432045661>"  # voice transcription module
CHECK = "<:check_100dp_FFFFFF_FILL1_wght700:1571618504141176872>"
BANNER = "<:banner:1571617396542939257>"
TEXT = "<:text:1571617390792540170>"
TRANSLATE = "<:translate:1571617524691247205>"
WEBHOOK = "<:webhook:1571617409582895146>"
FILTER = "<:filter:1571617535814672515>"
FOLDER = "<:folder:1571617546187178084>"
FOLDERS = "<:folders:1571617539518111845>"
IMAGE = "<:image:1571617548527730779>"
SHIELD = "<:shield:1571617552646410304>"  # automod icon
HAND = "<:hand:1571617556689584240>"  # appeal "claim" button
LINK = "<:link:1571617554932170964>"  # appeal "invite" button
PENDING = "<:pending:1571617550339547269>"  # hourglass: appeal pending, automod moderator approval pending
CLAIM = "<:claim:1571618002321809642>"  # take a ticket in charge
THREADS = "<:threads:1571617994029797489>"  # private staff thread

# Tools and services
COLOR = "<:color:1571617503073927329>"  # /color
LOCK = "<:lock:1571617484832768192>"  # /ssl (certificate)
DATA_OBJECT = "<:data_object:1571617433234571387>"  # JSON blocks (/jwt)
DICTIONARY = "<:dictionary:1571618457156321471>"  # /define — Material `dictionary`
REGEX = "<:regex:1571618450726715422>"  # /regex — Material `regular_expression`
TAG = "<:tag:1571618447811674152>"  # /hash — Material `tag`
DIFFERENCE = "<:difference:1571618441553518672>"  # /diff — Material `difference`
ANALYTICS = "<:analytics:1571618439603159221>"  # /textstats — Material `analytics`
NUMBERS = "<:number:1571618430703112383>"  # /uuid — Material `123`
TOKEN = "<:token:1571618423635574966>"  # /jwt — Material `token`
GPP_MAYBE = "<:gpp_maybe:1571618421618249789>"  # /pwned — Material `gpp_maybe`
MONITOR_HEART = "<:monitor_heart:1571618415255228540>"  # /status — Material `monitor_heart`
STATUS_OK = "<:check_circle:1571618466048376842>"  # /status: operational
STATUS_DEGRADED = "<:degraded:1571618460964884512>"  # /status: degraded / partial
STATUS_DOWN = "<:down:1571618463850692679>"  # /status: major outage
STATUS_MAINTENANCE = "<:maintenance:1571618459132100630>"  # /status: maintenance
BUSINESS = "<:business:1571618413128982538>"  # /company — Material `business`
DNS = "<:dns:1571618406254256139>"  # /dns — Material `dns`
ACCOUNT_TREE = "<:account_tree:1571618399522529462>"  # /subdomains — Material `account_tree`
ALT_ROUTE = "<:alt_route:1571618393176674344>"  # /link-check redirect chain — Material `alt_route`
PERSON_SEARCH = "<:person_search:1571618390957752332>"  # /username — Material `person_search`
TRANSFORM = "<:transform:1571618370632028190>"  # /convert — Material `transform`
FRAME_INSPECT = "<:frame_inspect:1571618361559744625>"  # /exif — Material `frame_inspect`
LOCATION_ON = "<:location_on:1571618359601270915>"  # /exif GPS — Material `location_on`
IMAGE_SEARCH = "<:image_search:1571618352915546353>"  # /sauce — Material `image_search`
MOVIE = "<:movie:1571618349497061469>"  # /sauce anime result — Material `movie`
MUSIC_NOTE = (
    "<:music_note:1571618343293685811>"  # /music + music links module — Material `music_note`
)
WIKTIONARY = "<:Wiktionary:1571618492397133945>"  # /define
OPENAI = "<:openai:1571618486273441923>"  # /tldr
DATA_GOUV = "<:data_gouv:1571618484281147522>"  # /company — Annuaire des Entreprises / data.gouv
CLOUDFLARE = "<:cloudflare:1571618482074689586>"  # /dns (DNS-over-HTTPS) + /status
TICKET = "<:ticket:1571618000899940352>"  # module icon + ticket cards
YOUTUBE = "<:youtube:1571617334584680642>"
TWITCH = "<:twitch:1571617332990705684>"
BLUESKY = "<:bluesky:1571617330914664573>"
RSS = "<:rss:1571617349780639836>"

# Extra icons of the design kit used by intercom
KEY = "<:key:1571617752655990854>"  # a permission
PERMISSIONS = "<:permissions:1571617782976618646>"  # permissions panel
OWNER = "<:owner:1571617778891366571>"  # hard-coded administrator
ADD_MEMBER = "<:addmember:1571617922709721149>"  # rank a member
REMOVE_MEMBER = "<:removemember:1571617691163168810>"  # unrank a member
ID = "<:ID:1571617741683695688>"
EXPLORE = "<:explore:1571617482899464314>"
IDEA = "<:idea:1571617507222229052>"
POLICY = "<:policy:1571617511156224140>"
EYE = "<:eye_m:1571617413596971068>"
NOTIFICATIONS = "<:notifications:1571617415349932042>"
NETWORK_PING = "<:network_ping:1571617398585434257>"
UPDATE = "<:update:1571617518794051776>"
COMMIT = "<:commit:1571617526679605369>"
LABEL = "<:label:1571617500855275542>"
TRACK = "<:track:1571617449487368283>"
COPYRIGHT = "<:copyright:1571617514616786974>"
TOGGLE = TOGGLE_ON
