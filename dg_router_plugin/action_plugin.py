"""KiCad Action Plugin registration (SWIG/pcbnew API).

This is the classic mechanism: KiCad discovers plugins in its scripting/plugins
dir, and each ActionPlugin becomes a toolbar button in the PCB editor.

Kept deliberately thin — all real work lives in shim.py (headless-safe) and
dialog.py (wx UI), so the logic is triggerable from code without the GUI.
"""

import os

import pcbnew


class DgRouterPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "dg-router (AI autorouter)"
        self.category = "Routing"
        self.description = (
            "AI-drivable incremental autorouter — Milestone 0 shim "
            "(inspect nets, preview render, emit job.json; no routing yet)"
        )
        self.show_toolbar_button = True
        icon = os.path.join(os.path.dirname(__file__), "icon.png")
        self.icon_file_name = icon
        self.dark_icon_file_name = icon

    def Run(self):
        # Reload submodules so edits take effect on "Refresh Plugins" without a
        # full KiCad restart (Python otherwise keeps the first import cached).
        import importlib
        from . import shim, router, dialog
        importlib.reload(shim)
        importlib.reload(router)
        importlib.reload(dialog)
        dialog.show_dialog(pcbnew.GetBoard())
