#!/bin/bash

echo "Starting Train Dispatcher App..."

sudo ufw disable

source .venv/bin/activate

python3 service_manager.py
