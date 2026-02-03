#!/usr/bin/env python3
"""
Network Sonification — Turn network traffic into ambient sound.

Each connected client becomes a tone. VLAN determines the base frequency range.
Traffic volume modulates amplitude. The result is a generative ambient soundscape
that reflects the current state of the network.

Usage:
    python3 network_sonification.py           # Generate 30s of ambient audio
    python3 network_sonification.py --live    # Continuous generation (future)
    python3 network_sonification.py --play    # Generate and play immediately
"""

import json
import math
import os
import struct
import subprocess
import sys
import urllib.request
import urllib.error
import ssl
import wave
from datetime import datetime

import numpy as np

# UniFi API config
UNIFI_HOST = "192.168.53.1"
UNIFI_USER = "nox"
UNIFI_PASS = "ONgbEc5oVWhDq1vLOpXKn99"

# Audio parameters
SAMPLE_RATE = 44100
DURATION = 30  # seconds
OUTPUT_DIR = os.path.expanduser("~/aispace/experiments/audio")

# VLAN -> frequency range mapping (Hz)
# Each VLAN gets a different "register" of the soundscape
VLAN_FREQUENCIES = {
    "192.168.53": (220, 440),    # Main LAN: A3-A4 (warm, centered)
    "192.168.55": (330, 660),    # IOT: E4-E5 (higher, electronic)
    "192.168.56": (165, 330),    # VPN Canada: E3-E4 (lower, distant)
    "192.168.54": (196, 392),    # VPN USA: G3-G4
    "192.168.57": (247, 494),    # VPN UK: B3-B4
    "192.168.58": (277, 554),    # TOR: C#4-C#5 (mysterious)
    "192.168.2":  (147, 294),    # WireGuard: D3-D4 (deep)
}

# Waveform types for variety
WAVEFORMS = ['sine', 'triangle', 'soft_square']


class UniFiClient:
    """Minimal UniFi API client."""

    def __init__(self):
        self.cookie = None
        self.csrf = None
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def login(self):
        url = f"https://{UNIFI_HOST}/api/auth/login"
        data = json.dumps({"username": UNIFI_USER, "password": UNIFI_PASS}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

        try:
            resp = urllib.request.urlopen(req, context=self.ctx)
            for cookie in resp.headers.get_all('Set-Cookie') or []:
                if 'TOKEN=' in cookie:
                    self.cookie = cookie.split(';')[0]
                    # Extract CSRF from JWT
                    import base64
                    token = self.cookie.split('=')[1]
                    payload = token.split('.')[1]
                    payload += '=' * (4 - len(payload) % 4)
                    decoded = json.loads(base64.urlsafe_b64decode(payload))
                    self.csrf = decoded.get('csrfToken', '')
            return True
        except Exception as e:
            print(f"Login failed: {e}")
            return False

    def get_clients(self):
        if not self.cookie:
            if not self.login():
                return []

        url = f"https://{UNIFI_HOST}/proxy/network/api/s/default/stat/sta"
        headers = {
            "Cookie": self.cookie,
            "X-CSRF-Token": self.csrf
        }
        req = urllib.request.Request(url, headers=headers)

        try:
            resp = urllib.request.urlopen(req, context=self.ctx)
            data = json.loads(resp.read().decode())
            return data.get('data', [])
        except Exception as e:
            print(f"Failed to get clients: {e}")
            return []


def get_vlan_from_ip(ip):
    """Extract VLAN prefix from IP address."""
    if not ip:
        return None
    parts = ip.rsplit('.', 1)
    return parts[0] if len(parts) == 2 else None


def generate_waveform(freq, duration, sample_rate, waveform='sine', amplitude=0.3):
    """Generate a waveform with gentle attack/release envelope."""
    t = np.linspace(0, duration, int(sample_rate * duration), False)

    if waveform == 'sine':
        wave = np.sin(2 * np.pi * freq * t)
    elif waveform == 'triangle':
        wave = 2 * np.abs(2 * (t * freq - np.floor(t * freq + 0.5))) - 1
    elif waveform == 'soft_square':
        # Square wave with smoothed edges
        wave = np.tanh(4 * np.sin(2 * np.pi * freq * t))
    else:
        wave = np.sin(2 * np.pi * freq * t)

    # Apply envelope: gentle fade in/out
    attack = int(sample_rate * 0.5)  # 500ms attack
    release = int(sample_rate * 1.0)  # 1s release
    envelope = np.ones(len(t))
    envelope[:attack] = np.linspace(0, 1, attack)
    envelope[-release:] = np.linspace(1, 0, release)

    return wave * envelope * amplitude


def add_subtle_modulation(wave, sample_rate, lfo_freq=0.1, depth=0.15):
    """Add slow amplitude modulation for organic movement."""
    t = np.linspace(0, len(wave) / sample_rate, len(wave), False)
    lfo = 1 + depth * np.sin(2 * np.pi * lfo_freq * t)
    return wave * lfo


def client_to_tone(client, duration, sample_rate):
    """Convert a network client to an audio tone."""
    ip = client.get('ip', '')
    vlan = get_vlan_from_ip(ip)

    # Get frequency range for this VLAN
    freq_range = VLAN_FREQUENCIES.get(vlan, (200, 400))

    # Use MAC address hash to get consistent frequency within range
    mac = client.get('mac', '00:00:00:00:00:00')
    mac_hash = hash(mac) % 1000 / 1000  # 0-1
    freq = freq_range[0] + mac_hash * (freq_range[1] - freq_range[0])

    # Traffic volume affects amplitude (log scale)
    tx_bytes = client.get('tx_bytes', 0)
    rx_bytes = client.get('rx_bytes', 0)
    total_bytes = tx_bytes + rx_bytes

    # Logarithmic scaling: 1KB -> 0.1, 1MB -> 0.2, 1GB -> 0.3, etc.
    if total_bytes > 0:
        amplitude = 0.05 + 0.05 * min(math.log10(total_bytes + 1) / 3, 1)
    else:
        amplitude = 0.02  # Very quiet for idle clients

    # Pick waveform based on device type or connection
    is_wired = client.get('is_wired', False)
    waveform = 'sine' if is_wired else np.random.choice(['sine', 'triangle', 'soft_square'])

    # LFO frequency varies per client (slow organic movement)
    lfo_freq = 0.05 + (hash(mac + 'lfo') % 100) / 1000  # 0.05-0.15 Hz

    # Generate the tone
    tone = generate_waveform(freq, duration, sample_rate, waveform, amplitude)
    tone = add_subtle_modulation(tone, sample_rate, lfo_freq)

    return tone, {
        'name': client.get('name') or client.get('hostname') or mac,
        'ip': ip,
        'freq': round(freq, 1),
        'amplitude': round(amplitude, 3),
        'waveform': waveform
    }


def generate_pad(duration, sample_rate, base_freq=55):
    """Generate a low ambient pad as foundation."""
    t = np.linspace(0, duration, int(sample_rate * duration), False)

    # Layer multiple detuned sines for richness
    pad = np.zeros(len(t))
    for detune in [-2, 0, 2, 7, 12]:  # Slight detuning + fifth + octave
        freq = base_freq * (2 ** (detune / 1200))  # Cents to frequency ratio
        pad += 0.02 * np.sin(2 * np.pi * freq * t)

    # Very slow modulation
    lfo = 1 + 0.3 * np.sin(2 * np.pi * 0.03 * t)
    pad *= lfo

    # Gentle envelope
    attack = int(sample_rate * 2)
    release = int(sample_rate * 3)
    envelope = np.ones(len(t))
    envelope[:attack] = np.linspace(0, 1, attack)
    envelope[-release:] = np.linspace(1, 0, release)

    return pad * envelope


def mix_and_normalize(tracks):
    """Mix multiple tracks and normalize."""
    if not tracks:
        return np.zeros(SAMPLE_RATE * DURATION)

    # Ensure all tracks are same length
    max_len = max(len(t) for t in tracks)
    padded = []
    for t in tracks:
        if len(t) < max_len:
            t = np.concatenate([t, np.zeros(max_len - len(t))])
        padded.append(t)

    # Sum all tracks
    mixed = np.sum(padded, axis=0)

    # Soft clip and normalize
    mixed = np.tanh(mixed * 0.7)  # Soft saturation
    peak = np.max(np.abs(mixed))
    if peak > 0:
        mixed = mixed / peak * 0.9  # Leave headroom

    return mixed


def save_wav(samples, filename, sample_rate=SAMPLE_RATE):
    """Save samples to WAV file."""
    os.makedirs(os.path.dirname(filename), exist_ok=True)

    # Convert to 16-bit PCM
    samples_16bit = (samples * 32767).astype(np.int16)

    with wave.open(filename, 'w') as wav:
        wav.setnchannels(1)  # Mono
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)
        wav.writeframes(samples_16bit.tobytes())

    return filename


def main():
    play_after = '--play' in sys.argv

    print("Network Sonification")
    print("=" * 40)

    # Fetch network clients
    print("Fetching network clients...")
    unifi = UniFiClient()
    clients = unifi.get_clients()

    if not clients:
        print("No clients found or API error. Generating test pattern.")
        # Generate a simple test tone
        test_tone = generate_waveform(440, DURATION, SAMPLE_RATE, 'sine', 0.3)
        test_tone = add_subtle_modulation(test_tone, SAMPLE_RATE)
        mixed = test_tone
    else:
        print(f"Found {len(clients)} clients")

        # Generate tones for each client
        tracks = []
        client_info = []

        for client in clients:
            tone, info = client_to_tone(client, DURATION, SAMPLE_RATE)
            tracks.append(tone)
            client_info.append(info)

        # Add ambient pad
        pad = generate_pad(DURATION, SAMPLE_RATE)
        tracks.append(pad)

        # Mix everything
        print(f"Mixing {len(tracks)} tracks...")
        mixed = mix_and_normalize(tracks)

        # Print client->tone mapping
        print("\nClient → Tone mapping:")
        for info in sorted(client_info, key=lambda x: x['freq']):
            print(f"  {info['name'][:20]:20} | {info['ip']:15} | {info['freq']:6.1f} Hz | {info['waveform']}")

    # Save the audio
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{OUTPUT_DIR}/network-{timestamp}.wav"
    save_wav(mixed, filename)
    print(f"\nSaved: {filename}")
    print(f"Duration: {DURATION}s")

    # Play if requested
    if play_after:
        print("Playing...")
        subprocess.run(['aplay', '-q', filename])

    return filename


if __name__ == '__main__':
    main()
