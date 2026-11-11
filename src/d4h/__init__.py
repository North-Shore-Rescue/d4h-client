"""Client for the D4H Team Manager v3 API."""
from .client import D4hClient
from .local import LocalD4hClient, save_lists

__all__ = ['D4hClient', 'LocalD4hClient', 'save_lists']
__version__ = '0.2.0'
