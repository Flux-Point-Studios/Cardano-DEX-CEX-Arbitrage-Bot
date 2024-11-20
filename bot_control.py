#!/usr/bin/env python3
"""
Bot Control Script for Cardano DEX-CEX Arbitrage Bot

This script provides command-line control interface for managing the arbitrage bot.
It handles starting, stopping, and checking the status of the bot process.

Usage:
    ./bot_control.py start   - Start the arbitrage bot
    ./bot_control.py stop    - Stop the bot gracefully
    ./bot_control.py status  - Check bot status and recent logs

Features:
    - Process management with PID file tracking
    - Graceful shutdown handling
    - Status monitoring with log viewing
    - Stale process cleanup
    - Directory structure verification

Environment:
    Requires 'run' and 'logs' directories in the same path as the script.
    Manages PID file at 'run/arbitrage_bot.pid'
    Logs are stored in 'logs/bot.log'

Dependencies:
    - Python 3.7+
    - Standard library only (no external packages required)

Known Issues:
    - PID file may remain if process is killed ungracefully
    - May need manual cleanup if bot crashes
    - Log rotation not handled automatically
"""

import os
import sys
import signal
import subprocess
import time

def read_pid() -> int:
    """
    Read the bot's PID from the PID file.
    
    Returns:
        int: Process ID if file exists and contains valid PID
        None: If file doesn't exist or is invalid
    
    Note:
        PID file is stored in 'run/arbitrage_bot.pid'
    """
    try:
        with open('run/arbitrage_bot.pid', 'r') as f:
            return int(f.read().strip())
    except FileNotFoundError:
        return None

def start() -> None:
    """
    Start the arbitrage bot process.
    
    Creates required directories if they don't exist.
    Checks for existing process before starting.
    Launches bot as a background process.
    
    Side Effects:
        - Creates 'logs' and 'run' directories if needed
        - Creates PID file when process starts
        - Prints status messages to console
    
    Known Issues:
        - No automatic recovery if bot fails to start
        - May need manual cleanup if PID file exists but process is dead
    """
    if read_pid():
        print("Bot appears to be running already!")
        return
        
    print("Starting arbitrage bot...")
    # Create required directories
    for dir in ['logs', 'run']:
        if not os.path.exists(dir):
            os.makedirs(dir)
        
    subprocess.Popen(['python3', 'arbitrage_bot.py'])
    time.sleep(2)  # Wait for process to start
    
    pid = read_pid()
    if pid:
        print(f"Bot started with PID: {pid}")
        print("Check logs/bot.log for details")
    else:
        print("Bot may have failed to start. Check logs/bot.log")

def stop() -> None:
    """
    Stop the bot process gracefully.
    
    Attempts graceful shutdown first (SIGTERM),
    follows with forced shutdown (SIGKILL) if needed.
    Cleans up PID file regardless of shutdown success.
    
    Side Effects:
        - Sends signals to bot process
        - Removes PID file
        - Prints status messages to console
    
    Known Issues:
        - May leave orphaned processes if bot spawns children
        - Might need manual intervention if bot hangs
    """
    pid = read_pid()
    if not pid:
        print("Bot does not appear to be running")
        # Clean up stale PID file if it exists
        if os.path.exists('run/arbitrage_bot.pid'):
            os.remove('run/arbitrage_bot.pid')
        return
        
    print(f"Stopping bot (PID: {pid})...")
    try:
        # Try graceful shutdown first
        os.kill(pid, signal.SIGTERM)
        
        # Wait for process to exit
        for _ in range(10):  # Wait up to 10 seconds
            try:
                os.kill(pid, 0)  # Check if process still exists
                time.sleep(1)
            except ProcessLookupError:
                break
        
        # Force kill if still running
        try:
            os.kill(pid, 0)
            print("Bot didn't stop gracefully, forcing...")
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
            
    except ProcessLookupError:
        print("Bot was not running")
    finally:
        # Always clean up PID file
        if os.path.exists('run/arbitrage_bot.pid'):
            os.remove('run/arbitrage_bot.pid')
        print("Bot stopped and cleanup completed")

def status() -> None:
    """
    Check and display bot status and recent logs.
    
    Shows:
        - Whether bot is running (with PID)
        - Last 10 lines of log file
        - Handles stale PID file cleanup
    
    Side Effects:
        - Reads PID file
        - Reads log file
        - Prints status to console
        - May remove stale PID file
    
    Known Issues:
        - Log reading may fail if file permissions are incorrect
        - Status may be inaccurate immediately after bot crash
    """
    pid = read_pid()
    if not pid:
        print("Bot is not running")
        return
        
    try:
        os.kill(pid, 0)
        print(f"Bot is running (PID: {pid})")
        
        # Show recent logs
        print("\nRecent log entries:")
        os.system("tail -n 10 logs/bot.log")
    except OSError:
        print("Bot crashed or was killed")
        if os.path.exists('run/arbitrage_bot.pid'):
            os.remove('run/arbitrage_bot.pid')

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ['start', 'stop', 'status']:
        print("Usage: ./bot_control.py [start|stop|status]")
        sys.exit(1)
        
    command = sys.argv[1]
    
    if command == 'start':
        start()
    elif command == 'stop':
        stop()
    elif command == 'status':
        status()
