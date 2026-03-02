# Deploy guide (server, 24/7)

## 1) Copy project to server
```bash
scp -r parser user@SERVER_IP:/opt/parser
ssh user@SERVER_IP
cd /opt/parser
```

## 2) Install Python deps (if needed)
`onus/onus.py` uses `requests`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip requests
```

## 3) Test run manually
```bash
python3 stack_runner.py --host 0.0.0.0 --port 8080
```

Open in browser from any device in network:
- `http://SERVER_IP:8080`

Each friend can open same URL and use their own UI controls (min spread, custom spread builder, etc.).

## 4) Run as system service (auto-start after reboot)
Edit `systemd/parser-stack.service`:
- `WorkingDirectory=/opt/parser`
- `ExecStart=` path to your python (venv or system)
- `User=` server user

Then:
```bash
sudo cp systemd/parser-stack.service /etc/systemd/system/parser-stack.service
sudo systemctl daemon-reload
sudo systemctl enable --now parser-stack
sudo systemctl status parser-stack
```

## 5) Useful commands
```bash
sudo journalctl -u parser-stack -f
ls -lah logs/
```

## 6) Open firewall port
If UFW enabled:
```bash
sudo ufw allow 8080/tcp
```

## Notes
- Collectors update DBs about every second, dashboard reads latest snapshots.
- For internet access (not only LAN), open port 8080 on your cloud firewall/security group.
- Optional: put Nginx in front + HTTPS if you plan public access.
