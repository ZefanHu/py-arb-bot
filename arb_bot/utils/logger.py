# arb_bot/utils/logger.py
import logging
import sys
from pathlib import Path

def setup_logger(name, level=logging.INFO, log_to_file=False, log_file_path="logs/arb_bot.log"):
    """
    Sets up a logger instance.

    Args:
        name (str): The name of the logger (usually __name__ of the calling module).
        level (int): The logging level (e.g., logging.INFO, logging.DEBUG).
        log_to_file (bool): Whether to log to a file in addition to the console.
        log_file_path (str): Path to the log file if log_to_file is True.

    Returns:
        logging.Logger: Configured logger instance.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False # Prevents log messages from being passed to the root logger

    # Create formatter
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                                  datefmt='%Y-%m-%d %H:%M:%S')

    # Console Handler
    if not logger.handlers: # Add handlers only if they don't exist
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(level)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

        # File Handler (optional)
        if log_to_file:
            # Ensure logs directory exists
            # Assuming this script is in arb_bot/utils/ and logs is in arb_bot/logs/
            try:
                # Path relative to this file's location if needed, or use absolute/settings-defined path
                log_dir = Path(__file__).resolve().parent.parent / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                file_path = log_dir / Path(log_file_path).name # Use only the filename part
            except NameError: # __file__ might not be defined in some contexts
                file_path = Path(log_file_path)
                file_path.parent.mkdir(parents=True, exist_ok=True)


            fh = logging.FileHandler(file_path)
            fh.setLevel(level)
            fh.setFormatter(formatter)
            logger.addHandler(fh)
            logger.info(f"Logging to file: {file_path.resolve()}")

    return logger

if __name__ == '__main__':
    # Example usage:
    # Assuming logs directory is a sibling of utils, or path is absolute/configured
    # For this example, let's assume logs is at ../logs relative to this file
    test_log_path = "../logs/test_logger.log" # Adjust if your structure is different for testing
    
    logger1 = setup_logger("my_app_test", logging.DEBUG, log_to_file=True, log_file_path=test_log_path)
    logger1.debug("This is a debug message from logger1.")
    logger1.info("This is an info message from logger1.")

    logger2 = setup_logger("another_module_test", logging.INFO)
    logger2.info("Info message from logger2 (console only).")
    logger2.warning("Warning message from logger2.")

