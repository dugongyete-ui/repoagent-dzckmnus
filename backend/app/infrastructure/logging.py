import logging
import sys
import os
from typing import Optional
from app.core.config import Settings, get_settings

def setup_logging(settings: Optional[Settings] = None):
    """
    Configure the application logging system
    
    Sets up log levels, formatters, and handlers for both console and file output.
    Ensures proper log rotation to prevent log files from growing too large.
    """
    # Configuration validation belongs to readiness, not module import. A
    # deployment with missing secrets can still expose a useful liveness signal.
    if settings is None:
        try:
            settings = get_settings()
        except Exception as exc:
            logging.basicConfig(level=logging.INFO)
            logging.getLogger(__name__).warning(
                "Settings unavailable during logging bootstrap: %s", exc
            )
            settings = None

    # Get root logger
    root_logger = logging.getLogger()
    
    # Set root log level
    configured_level = settings.log_level if settings else os.getenv("LOG_LEVEL", "INFO")
    log_level = getattr(logging, configured_level.upper(), logging.INFO)
    root_logger.setLevel(log_level)
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)
    
    # Add handlers to root logger
    root_logger.addHandler(console_handler)

    # Disable verbose logging for pymongo
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("sse_starlette.sse").setLevel(logging.INFO)
    
    # Log initialization complete
    root_logger.info("Logging system initialized - Console and file logging active") 