"""Client for the D4H Team Manager v3 API."""
from .client import D4hClient
from .local import LocalD4hClient

__all__ = ['D4hClient', 'LocalD4hClient']
__version__ = '0.1.0'
