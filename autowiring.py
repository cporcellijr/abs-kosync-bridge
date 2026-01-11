#!/usr/bin/env python3
"""
Spring-like autowiring decorators for Python dependency injection.
Provides @Autowired and @Component functionality.
"""

import inspect
import logging
from typing import Type, TypeVar, Callable, Any
from functools import wraps

logger = logging.getLogger(__name__)

# Global registry for autowired components
_autowired_registry = {}
_component_registry = {}

T = TypeVar('T')


def Autowired(interface: Type = None):
    """
    Decorator to mark a parameter or field for autowiring.
    Usage:
        @Autowired(ABSClient)
        def some_function(abs_client: ABSClient): ...

        OR in constructor:
        def __init__(self, abs_client: ABSClient = Autowired()):
    """
    if interface is None:
        # Used as default value in constructor: abs_client: ABSClient = Autowired()
        class AutowiredMarker:
            def __init__(self, interface_type=None):
                self.interface_type = interface_type
        return AutowiredMarker(None)
    else:
        # Used as decorator: @Autowired(ABSClient)
        def decorator(func_or_class):
            if inspect.isclass(func_or_class):
                _autowired_registry[func_or_class] = interface
                return func_or_class
            else:
                @wraps(func_or_class)
                def wrapper(*args, **kwargs):
                    # Inject dependency if not provided
                    sig = inspect.signature(func_or_class)
                    bound_args = sig.bind_partial(*args, **kwargs)

                    # Find autowired parameters
                    for param_name, param in sig.parameters.items():
                        if param_name not in bound_args.arguments:
                            if param.annotation in _component_registry:
                                kwargs[param_name] = _component_registry[param.annotation]

                    return func_or_class(*args, **kwargs)
                return wrapper
        return decorator


def Component(interface: Type = None):
    """
    Decorator to mark a class as a component that can be autowired.
    Usage:
        @Component()
        class MyService:
            pass

        @Component(interface=IMyService)
        class MyServiceImpl(IMyService):
            pass
    """
    def decorator(cls):
        # Register the component
        registration_key = interface or cls
        _component_registry[registration_key] = cls

        # Mark the class as autowired-enabled
        _autowired_registry[cls] = registration_key

        return cls
    return decorator


def autowire_constructor(container, cls):
    """
    Automatically wire constructor dependencies using the DI container.
    This replaces the manual _create_with_autowiring in DIContainer.
    """
    sig = inspect.signature(cls.__init__)
    kwargs = {}

    for param_name, param in sig.parameters.items():
        if param_name == 'self':
            continue

        # Check if parameter has Autowired default value
        if hasattr(param.default, 'interface_type'):
            # This parameter was marked with Autowired()
            interface_type = param.annotation if param.annotation != inspect.Parameter.empty else None
            if interface_type:
                try:
                    kwargs[param_name] = container.get(interface_type)
                    continue
                except Exception as e:
                    if param.default == inspect.Parameter.empty:
                        logger.error(f"Cannot autowire {param_name}: {interface_type} for {cls.__name__}")
                        raise ValueError(f"Cannot autowire {param_name} for {cls.__name__}: {e}")

        # Check for type annotation
        elif param.annotation != inspect.Parameter.empty:
            try:
                kwargs[param_name] = container.get(param.annotation)
            except Exception as e:
                # If dependency can't be resolved and no default, check if it's optional
                if param.default == inspect.Parameter.empty:
                    logger.error(f"Cannot resolve dependency {param_name}: {param.annotation} for {cls.__name__}")
                    raise ValueError(f"Cannot autowire {param_name} for {cls.__name__}: {e}")
                # Use default value if available
                continue
        else:
            # Check for config values by parameter name
            if hasattr(container, 'get_config_value') and container.get_config_value(param_name) is not None:
                kwargs[param_name] = container.get_config_value(param_name)
            elif param.default == inspect.Parameter.empty:
                logger.warning(f"No type annotation or config value for {param_name} in {cls.__name__}")

    return cls(**kwargs)


def get_autowired_registry():
    """Get the current autowired registry."""
    return _autowired_registry.copy()


def get_component_registry():
    """Get the current component registry."""
    return _component_registry.copy()


def clear_registries():
    """Clear all registries (useful for testing)."""
    global _autowired_registry, _component_registry
    _autowired_registry.clear()
    _component_registry.clear()
