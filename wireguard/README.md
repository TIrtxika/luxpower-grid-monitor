# WireGuard Setup

## Generate Keys

### On VPS:
```bash
# Generate VPS keypair
wg genkey | tee vps_private.key | wg pubkey > vps_public.key
```

### On RPi:
```bash
# Generate RPi keypair
wg genkey | tee rpi_private.key | wg pubkey > rpi_public.key
```

## Exchange Public Keys

1. Copy VPS public key to RPi config
2. Copy RPi public key to VPS config

## Install WireGuard

### On VPS (Debian):
```bash
sudo apt update
sudo apt install wireguard

# Copy config
sudo cp vps-wg0.conf /etc/wireguard/wg0.conf
sudo chmod 600 /etc/wireguard/wg0.conf

# Edit config with your keys
sudo nano /etc/wireguard/wg0.conf

# Enable and start
sudo systemctl enable wg-quick@wg0
sudo systemctl start wg-quick@wg0

# Open firewall port
sudo ufw allow 51820/udp
```

### On RPi:
```bash
sudo apt update
sudo apt install wireguard

# Copy config
sudo cp rpi-wg0.conf /etc/wireguard/wg0.conf
sudo chmod 600 /etc/wireguard/wg0.conf

# Edit config with your keys and VPS IP
sudo nano /etc/wireguard/wg0.conf

# Enable and start
sudo systemctl enable wg-quick@wg0
sudo systemctl start wg-quick@wg0
```

## Verify Connection

### On VPS:
```bash
# Check WireGuard status
sudo wg show

# Ping RPi
ping 10.10.0.2
```

### On RPi:
```bash
# Check WireGuard status
sudo wg show

# Ping VPS
ping 10.10.0.1
```

## Troubleshooting

### Connection not establishing:
1. Check firewall on VPS allows UDP 51820
2. Verify VPS public IP in RPi config
3. Check keys are correct

### Intermittent connection:
1. Ensure PersistentKeepalive is set
2. Check for NAT issues

### Check logs:
```bash
sudo journalctl -u wg-quick@wg0 -f
```
