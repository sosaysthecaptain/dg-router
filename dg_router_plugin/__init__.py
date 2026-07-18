"""dg-router Action Plugin package.

KiCad imports this package on startup and expects it to register the plugin.
"""

from .action_plugin import DgRouterPlugin

DgRouterPlugin().register()
