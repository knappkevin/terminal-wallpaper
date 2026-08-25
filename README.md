# Terminal Wallpaper Plugin for Omarchy

Run a terminal as part of your desktop wallpaper. Double-click the wallpaper to configure the wallpaper image and terminal output. 

## Features

- **Double-click to configure**
- **Terminal color palette and backgrounds hot-swap with theme switch**
- **Terminal output overlays onto theme wallpaper** 

![donut](/examples/donut.gif)


## Install

```sh
omarchy pkg add vte3 gtk-layer-shell python-gobject
```
```sh
omarchy plugin add https://github.com/knappkevin/terminal-wallpaper.git --enable
```

## [Example Pictures Here](/examples)

## Example Commands

Omarchy screensaver overlay
```sh
while true; do ttfx -i ~/.config/omarchy/branding/screensaver.txt --frame-rate 60 --canvas-width 0 --canvas-height 0 --reuse-canvas --anchor-canvas c --anchor-text c --random-effect --no-eol --no-restore-cursor; done
```

Know the risks of downloading from aur
```sh
fastfetch                       # larp
btop                            # system monitor TUI
journalctl -f -n 40 --no-pager -o short   # live log
cmatrix                         # matrix rain
donut                           # from aur donut.c
asciiquarium --transparent      # from aur asciiquarium-git
cbonsai -li                     # from aur cbonsai
game of life                    # from idk

```

## Runtime IPC

```sh
omarchy-shell terminal-wallpaper open         # open config menu
omarchy-shell terminal-wallpaper close
omarchy-shell terminal-wallpaper toggle
omarchy-shell terminal-wallpaper refresh      # restart the terminal
omarchy-shell terminal-wallpaper setCommand 'fastfetch'   # change command
```

## Remove

```sh
omarchy plugin remove io.github.knappkevin.terminal-wallpaper
```
