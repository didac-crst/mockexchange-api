# src/mockexchange/logging_config.py  (or just at the top of market.py)
import logging
import sys

logging.basicConfig(
    level=logging.INFO,                 # DEBUG for more verbosity
    stream=sys.stdout,                  # log to container stdout
    format="%(asctime)s  %(levelname)s  %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)   # module-local logger
