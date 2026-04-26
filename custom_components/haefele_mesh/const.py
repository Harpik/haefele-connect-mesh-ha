"""Constants for Häfele Connect Mesh integration."""

DOMAIN = "haefele_mesh"

CONF_NETWORK_KEY = "network_key"
CONF_APP_KEY = "app_key"
CONF_IV_INDEX = "iv_index"
CONF_NODES = "nodes"

# BLE
MESH_PROXY_SERVICE_UUID  = "00001828-0000-1000-8000-00805f9b34fb"
MESH_PROXY_DATA_IN_UUID  = "00002add-0000-1000-8000-00805f9b34fb"
MESH_PROXY_DATA_OUT_UUID = "00002ade-0000-1000-8000-00805f9b34fb"

# Heartbeat
HEARTBEAT_INTERVAL = 60  # seconds

# Mesh crypto
IV_INDEX_DEFAULT = 1
# Base address used for our BT Mesh SRC.
#
# Must NOT collide with SRCs used by any previous provisioner/gateway for the
# same network, or the lights' anti-replay cache will silently drop our
# frames until our SEQ exceeds whatever they last accepted. The Pi3 gateway
# used 0x0060/0x0080, and the Haefele mobile app uses the provisioner
# address from the .connect file (typically 0x7FFD). 0x00C0+ is fresh in all
# known deployments, leaving 0x10 headroom between nodes.
# Our mesh SRC. Experimentally set to the provisioner address from the
# Häfele app (0x7FFD in casa-2.connect). Häfele's proxy appears to only
# deliver inbound unicast replies when the DST belongs to a node that
# was provisioned in its world — our own ad-hoc addresses (0x00C0 etc.)
# never saw a single Status reply across dozens of polling attempts.
# WARNING: this conflicts with the Häfele app if it's running at the
# same time (SEQ collisions on 0x7FFD). Keep the app closed.
SRC_ADDRESS_BASE = 0x7FFD

# Lower bound used when seeding a brand-new SEQ counter. Starting at
# 0x800000 (half the 24-bit SEQ space) guarantees we're above anything any
# previous emitter could plausibly have left in the lights' replay cache
# for this SRC, while still leaving ~8M emissions before we need an IV
# Index update — well beyond the lifetime of any install.
SEQ_SEED_MIN = 0x800000

# Light capabilities
LIGHT_MIN_KELVIN = 2700
LIGHT_MAX_KELVIN = 5000
