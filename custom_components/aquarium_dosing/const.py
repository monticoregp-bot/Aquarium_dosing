"""Constants for Aquarium Dosing."""

DOMAIN = "aquarium_dosing"
VERSION = "0.1.0"

CONF_MASTER_ENTITY = "master_entity"  # input_boolean: ON = scheduled dosing allowed
CONF_ONE_AT_A_TIME = "one_at_a_time"
CONF_LATE_TOLERANCE = "late_tolerance_minutes"
CONF_INITIAL_PUMP = "initial_pump"
CONF_INITIAL_SWITCH = "initial_switch"

# Used if an entry has no master entity stored.
DEFAULT_MASTER_ENTITY = "input_boolean.aquarium_dosing"
DEFAULT_LATE_TOLERANCE = 5

STORAGE_VERSION = 1
EVENT_DOSING = f"{DOMAIN}_event"
FRONTEND_URL = "/aquarium_dosing/aquarium-dosing-card.js"
