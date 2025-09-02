# Neptune Sauna Booking Script

Automated booking system for BPSB sauna reservations with complete Romanian interface.

## Quick Start

To run this project, simply use:

```bash
make run
```

## Available Commands

- `make install` - Install dependencies with UV
- `make run` - Run the Neptune booking script
- `make clean` - Clean UV cache and lock files
- `make help` - Show all available commands

## Shell Alias

For convenient access from anywhere, use the `neptun` command:

```bash
neptun
```

This alias navigates to the project directory and runs `make run` automatically.

## Features

- 🇷🇴 Complete Romanian interface
- 🛡️ Graceful error handling for invalid subscriptions
- 📅 Automatic calendar navigation
- 🎯 Multi-slot booking support
- ⚡ Modern UV package management