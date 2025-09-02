# Neptune Booking Script - Technical Patterns & Architecture

## Project Overview
Python Selenium-based automation script for BPSB sauna booking system with comprehensive Romanian localization.

## Key Technical Patterns

### Error Handling Architecture
- **Layered Validation**: Subscription → Reservations → Slots → Processing
- **Graceful Degradation**: CSS selectors → XPath fallbacks → specific Romanian message detection
- **User-Friendly Messaging**: Technical errors translated to actionable Romanian guidance

### Selenium Web Automation Patterns
- **XPath Strategy**: Absolute XPaths for specific booking system elements
- **Wait Patterns**: WebDriverWait with expected conditions for reliable element interaction
- **State Management**: Cart operations, slot selection sequencing, final processing workflow

### Package Management Evolution
- **Migration Pattern**: pip/venv → UV/UVX for modern Python dependency management
- **Build System**: Makefile-based commands for consistent development workflow
- **Shell Integration**: Custom aliases for user convenience and accessibility

### Translation Implementation
- **Systematic Approach**: User-facing messages only, preserving technical debug info
- **Romanian Localization**: Complete UI translation while maintaining code readability
- **Syntax Safety**: Careful handling of multiline strings and escape characters

### Booking Flow Architecture
```
User Selection → Error Detection → Capacity Check → Calendar Navigation → 
Date Selection → Slot Selection → Validation → Processing → Completion
```

### Code Organization Principles
- **Separation of Concerns**: Error detection, validation, UI interaction, and processing logic separated
- **Defensive Programming**: Multiple validation layers and graceful error handling
- **User Experience Focus**: Romanian interface with clear error explanations and guidance

## Development Workflow
1. **UV Package Management**: Modern Python dependency handling
2. **Make-based Commands**: Standardized development operations
3. **Shell Integration**: User-friendly access patterns
4. **Error-First Design**: Comprehensive error handling before feature implementation

## Testing Philosophy
- **Syntax Validation**: Python compilation checks before runtime
- **User Experience Testing**: Romanian interface verification
- **Error Scenario Coverage**: Invalid subscription handling and edge cases