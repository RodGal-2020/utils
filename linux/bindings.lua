-- Keep only your personal keybinding overrides here. Add new bindings or
-- unbind defaults before replacing them.

-- See current bindings and descriptions:
--   omarchy menu keybindings --print

-- To disable every Omarchy default binding, set this in
-- ~/.config/hypr/hyprland.lua before require("default.hypr.omarchy"), then add
-- only the bindings you want below:
--   omarchy_default_bindings = false

-- To disable all preinstalled app/webapp bindings, set:
--   omarchy_preinstalled_bindings = false

-- Add a new binding.
-- o.bind("SUPER + SHIFT + R", "SSH", "alacritty -e ssh your-server")

-- Change an existing binding by unbinding it first, then binding the key again.
-- This example changes SUPER+SPACE from the launcher to the Omarchy root menu.
-- hl.unbind("SUPER + SPACE")
-- o.bind("SUPER + SPACE", "Omarchy menu", "omarchy-menu toggle root")
hl.unbind("SUPER + SHIFT + E")
hl.unbind("SUPER + SHIFT + T")
hl.unbind("SUPER + SHIFT + O")
hl.unbind("SUPER + SHIFT + C")

o.bind(
	"SUPER + SHIFT + E",
	"Outlook",
	"chromium --new-window https://outlook.cloud.microsoft/mail/ https://mail.google.com/mail/u/0/#inbox"
)
o.bind(
	"SUPER + SHIFT + T",
	"Telegram + WhatsApp",
	"chromium --new-window https://web.telegram.org https://web.whatsapp.com"
)
-- o.bind("SUPER + SHIFT + O", "OneDrive", "chromium --new-window https://onedrive.live.com")
o.bind("SUPER + SHIFT + O", "OneDrive", "bash -c '~/.local/bin/mount-onedrive && nautilus ~/OneDrive'")
o.bind(
	"SUPER + SHIFT + C",
	"Todoist + Calendar",
	"chromium --new-window https://app.todoist.com/app/today https://calendar.google.com/calendar/"
)

-- Disable a default binding without replacing it.
-- hl.unbind("SUPER + SHIFT + B")

-- Logitech MX Keys examples:
-- o.bind("SUPER + SHIFT + S", nil, "omarchy-capture-screenshot")
-- o.bind("SUPER + H", nil, "voxtype record toggle")
-- o.bind("SUPER + PERIOD", nil, "omarchy-shell shell toggle omarchy.emojis")
