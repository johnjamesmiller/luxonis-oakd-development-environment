#!/bin/bash
mkdir -p ~/.ssh
cp -r /root/.ssh-from-host/* ~/.ssh/
sudo chown -R $(id -u):$(id -g) ~/.ssh