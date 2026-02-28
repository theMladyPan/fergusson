#!/bin/bash
mkdir -p ~/.config/systemd/user/
cp script/fergusson.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable fergusson
systemctl --user start fergusson
echo "Fergusson service installed and started."
