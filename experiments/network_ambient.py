#!/usr/bin/env python3
"""
Network Ambient — Evolving generative music from network topology.

A more musical take on network sonification. Instead of just mapping
clients to tones, this creates an evolving ambient piece with:
- Harmonic relationships based on network topology
- Chord progressions that shift over time
- Rhythmic pulses from traffic patterns
- Sections that build and release

Usage:
    python3 network_ambient.py [--duration 180] [--play]
"""

import json
import math
import os
import subprocess
import sys
import urllib.request
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
DEFAULT_DURATION = 180  # 3 minutes
OUTPUT_DIR = os.path.expanduser("~/aispace/experiments/audio")

# Musical constants (A minor pentatonic for ambient feel)
# A C D E G = 440, 523.25, 587.33, 659.25, 783.99
SCALE_RATIOS = [1, 6/5, 4/3, 3/2, 9/5]  # Minor pentatonic intervals
BASE_FREQ = 110  # A2


class UniFiClient:
    """Minimal UniFi API client."""

    def __init__(self):
        self.cookie = None
        self.csrf = None
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def login(self):
        import base64
        url = f"https://{UNIFI_HOST}/api/auth/login"
        data = json.dumps({"username": UNIFI_USER, "password": UNIFI_PASS}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

        try:
            resp = urllib.request.urlopen(req, context=self.ctx)
            for cookie in resp.headers.get_all('Set-Cookie') or []:
                if 'TOKEN=' in cookie:
                    self.cookie = cookie.split(';')[0]
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
        if not self.cookie and not self.login():
            return []

        url = f"https://{UNIFI_HOST}/proxy/network/api/s/default/stat/sta"
        headers = {"Cookie": self.cookie, "X-CSRF-Token": self.csrf}
        req = urllib.request.Request(url, headers=headers)

        try:
            resp = urllib.request.urlopen(req, context=self.ctx)
            return json.loads(resp.read().decode()).get('data', [])
        except Exception as e:
            print(f"Failed to get clients: {e}")
            return []


def scale_degree_to_freq(degree, octave=0):
    """Convert scale degree (0-4) to frequency."""
    ratio = SCALE_RATIOS[degree % len(SCALE_RATIOS)]
    octave_mult = 2 ** (octave + degree // len(SCALE_RATIOS))
    return BASE_FREQ * ratio * octave_mult


def generate_tone(freq, duration, sample_rate, attack=0.5, release=1.0, amp=0.3):
    """Generate a tone with envelope."""
    samples = int(sample_rate * duration)
    t = np.linspace(0, duration, samples, False)

    # Multiple harmonics for richness
    tone = np.zeros(samples)
    for harmonic, weight in [(1, 1.0), (2, 0.3), (3, 0.1), (4, 0.05)]:
        tone += weight * np.sin(2 * np.pi * freq * harmonic * t)

    # Normalize harmonic mix
    tone = tone / 1.45

    # ADSR envelope
    attack_samples = int(sample_rate * attack)
    release_samples = int(sample_rate * release)

    envelope = np.ones(samples)
    if attack_samples > 0:
        envelope[:attack_samples] = np.linspace(0, 1, attack_samples)
    if release_samples > 0 and release_samples < samples:
        envelope[-release_samples:] = np.linspace(1, 0, release_samples)

    return tone * envelope * amp


def generate_pad_chord(degrees, duration, sample_rate, amp=0.15):
    """Generate a sustained pad chord."""
    samples = int(sample_rate * duration)
    chord = np.zeros(samples)

    for degree in degrees:
        freq = scale_degree_to_freq(degree, octave=0)
        tone = generate_tone(freq, duration, sample_rate, attack=2.0, release=3.0, amp=amp)

        # Add octave doubling for fullness
        upper = generate_tone(freq * 2, duration, sample_rate, attack=3.0, release=4.0, amp=amp * 0.3)

        chord += tone + upper

    # Slow modulation
    t = np.linspace(0, duration, samples, False)
    lfo = 1 + 0.2 * np.sin(2 * np.pi * 0.05 * t)
    chord *= lfo

    return chord


def generate_arpeggio(degrees, duration, sample_rate, pattern='up', speed=0.5):
    """Generate an arpeggiated pattern."""
    samples = int(sample_rate * duration)
    arp = np.zeros(samples)

    note_duration = speed
    note_samples = int(sample_rate * note_duration)

    if pattern == 'up':
        note_sequence = degrees
    elif pattern == 'down':
        note_sequence = degrees[::-1]
    elif pattern == 'updown':
        note_sequence = degrees + degrees[-2:0:-1]
    else:
        note_sequence = degrees

    num_notes = int(duration / note_duration)

    for i in range(num_notes):
        degree = note_sequence[i % len(note_sequence)]
        freq = scale_degree_to_freq(degree, octave=1)  # Higher octave for arp

        start = i * note_samples
        if start + note_samples > samples:
            break

        note = generate_tone(freq, note_duration, sample_rate,
                           attack=0.05, release=0.3, amp=0.12)
        arp[start:start + len(note)] += note

    return arp


def generate_bass_pulse(freq, duration, sample_rate, pulse_rate=0.25):
    """Generate a pulsing bass note."""
    samples = int(sample_rate * duration)
    t = np.linspace(0, duration, samples, False)

    # Sub bass
    bass = np.sin(2 * np.pi * freq * t) * 0.2

    # Pulse envelope
    pulse_samples = int(sample_rate * pulse_rate)
    pulse_env = np.zeros(samples)
    for i in range(0, samples, pulse_samples * 2):
        end = min(i + pulse_samples, samples)
        pulse_env[i:end] = np.linspace(0.8, 0.3, end - i)

    bass *= pulse_env

    # Slow overall envelope
    attack = int(sample_rate * 4)
    release = int(sample_rate * 4)
    envelope = np.ones(samples)
    envelope[:attack] = np.linspace(0, 1, attack)
    envelope[-release:] = np.linspace(1, 0, release)

    return bass * envelope


def client_to_musical_element(client, duration, sample_rate, position_in_piece):
    """Convert a client to a musical element based on its properties."""
    mac = client.get('mac', '00:00:00:00:00:00')
    tx_bytes = client.get('tx_bytes', 0)
    rx_bytes = client.get('rx_bytes', 0)
    total_traffic = tx_bytes + rx_bytes

    # Hash MAC to get consistent musical properties
    mac_hash = hash(mac)

    # Determine scale degree from MAC
    degree = mac_hash % 5  # 0-4 in pentatonic scale

    # Octave based on traffic (more traffic = higher octave)
    if total_traffic > 1e9:  # > 1GB
        octave = 2
    elif total_traffic > 1e6:  # > 1MB
        octave = 1
    else:
        octave = 0

    # Amplitude based on traffic (log scale)
    if total_traffic > 0:
        amp = 0.05 + 0.1 * min(math.log10(total_traffic + 1) / 10, 1)
    else:
        amp = 0.02

    # Generate a sustained tone
    freq = scale_degree_to_freq(degree, octave)
    tone = generate_tone(freq, duration, sample_rate, attack=1.0, release=2.0, amp=amp)

    # Add vibrato for organic feel
    t = np.linspace(0, duration, len(tone), False)
    vibrato_rate = 4 + (mac_hash % 20) / 10  # 4-6 Hz
    vibrato_depth = 0.002
    vibrato = np.sin(2 * np.pi * vibrato_rate * t) * vibrato_depth
    # Apply pitch vibrato by resampling
    # (simplified: amplitude modulation as approximation)
    tone *= (1 + vibrato)

    return tone, degree


def generate_section(clients, duration, sample_rate, section_type='intro'):
    """Generate a section of the piece."""
    samples = int(sample_rate * duration)
    section = np.zeros(samples)

    # Chord progressions (scale degrees)
    PROGRESSIONS = {
        'intro': [[0, 2, 4], [0, 2, 4]],  # i chord sustained
        'build': [[0, 2, 4], [3, 0, 2], [4, 2, 0], [2, 4, 0]],  # i - VI - VII - v
        'peak': [[0, 2, 4], [1, 3, 0], [2, 4, 1], [4, 1, 3]],  # More movement
        'release': [[4, 2, 0], [2, 0, 4], [0, 2, 4]],  # Settling back
    }

    progression = PROGRESSIONS.get(section_type, PROGRESSIONS['intro'])
    chord_duration = duration / len(progression)

    # Add pad chords
    for i, degrees in enumerate(progression):
        start = int(i * chord_duration * sample_rate)
        chord = generate_pad_chord(degrees, chord_duration, sample_rate, amp=0.12)
        end = start + len(chord)
        if end > samples:
            chord = chord[:samples - start]
            end = samples
        section[start:end] += chord

    # Add bass
    bass_degree = progression[0][0]
    bass_freq = scale_degree_to_freq(bass_degree, octave=-1)
    bass = generate_bass_pulse(bass_freq, duration, sample_rate)
    section += bass

    # Add arpeggios in build and peak sections
    if section_type in ['build', 'peak']:
        arp_degrees = progression[0]
        pattern = 'updown' if section_type == 'peak' else 'up'
        speed = 0.3 if section_type == 'peak' else 0.5
        arp = generate_arpeggio(arp_degrees, duration, sample_rate, pattern=pattern, speed=speed)
        section += arp * 0.7

    # Add client tones (ambient texture)
    client_mix = np.zeros(samples)
    for client in clients[:8]:  # Limit to 8 for clarity
        tone, _ = client_to_musical_element(client, duration, sample_rate, 0)
        if len(tone) < samples:
            tone = np.concatenate([tone, np.zeros(samples - len(tone))])
        client_mix += tone[:samples]

    section += client_mix * 0.5

    return section


def generate_piece(clients, duration, sample_rate):
    """Generate the complete ambient piece."""
    samples = int(sample_rate * duration)

    # Define sections
    intro_dur = duration * 0.2
    build_dur = duration * 0.3
    peak_dur = duration * 0.25
    release_dur = duration * 0.25

    print(f"  Generating intro ({intro_dur:.0f}s)...")
    intro = generate_section(clients, intro_dur, sample_rate, 'intro')

    print(f"  Generating build ({build_dur:.0f}s)...")
    build = generate_section(clients, build_dur, sample_rate, 'build')

    print(f"  Generating peak ({peak_dur:.0f}s)...")
    peak = generate_section(clients, peak_dur, sample_rate, 'peak')

    print(f"  Generating release ({release_dur:.0f}s)...")
    release = generate_section(clients, release_dur, sample_rate, 'release')

    # Concatenate with crossfades
    crossfade = int(sample_rate * 2)  # 2 second crossfade

    piece = np.zeros(samples)

    # Intro
    intro_end = len(intro)
    piece[:intro_end] = intro

    # Build (crossfade from intro)
    build_start = intro_end - crossfade
    for i, sample in enumerate(build):
        pos = build_start + i
        if pos >= samples:
            break
        if i < crossfade:
            # Crossfade region
            fade = i / crossfade
            piece[pos] = piece[pos] * (1 - fade) + sample * fade
        else:
            piece[pos] = sample

    # Peak
    peak_start = build_start + len(build) - crossfade
    for i, sample in enumerate(peak):
        pos = peak_start + i
        if pos >= samples:
            break
        if i < crossfade:
            fade = i / crossfade
            piece[pos] = piece[pos] * (1 - fade) + sample * fade
        else:
            piece[pos] = sample

    # Release
    release_start = peak_start + len(peak) - crossfade
    for i, sample in enumerate(release):
        pos = release_start + i
        if pos >= samples:
            break
        if i < crossfade:
            fade = i / crossfade
            piece[pos] = piece[pos] * (1 - fade) + sample * fade
        else:
            piece[pos] = sample

    # Final fade out
    final_fade = int(sample_rate * 5)
    piece[-final_fade:] *= np.linspace(1, 0, final_fade)

    # Normalize
    piece = np.tanh(piece * 0.8)
    peak_val = np.max(np.abs(piece))
    if peak_val > 0:
        piece = piece / peak_val * 0.85

    return piece


def save_wav(samples, filename, sample_rate=SAMPLE_RATE):
    """Save samples to WAV file."""
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    samples_16bit = (samples * 32767).astype(np.int16)

    with wave.open(filename, 'w') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples_16bit.tobytes())

    return filename


def main():
    duration = DEFAULT_DURATION
    play_after = False

    # Parse args
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--duration' and i + 1 < len(args):
            duration = int(args[i + 1])
            i += 2
        elif args[i] == '--play':
            play_after = True
            i += 1
        else:
            i += 1

    print("Network Ambient")
    print("=" * 50)
    print(f"Duration: {duration}s")

    # Fetch clients
    print("Fetching network clients...")
    unifi = UniFiClient()
    clients = unifi.get_clients()

    if not clients:
        print("No clients found. Using minimal piece.")
        clients = []

    print(f"Found {len(clients)} clients")

    # Generate the piece
    print("Generating ambient piece...")
    piece = generate_piece(clients, duration, SAMPLE_RATE)

    # Save
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    wav_file = f"{OUTPUT_DIR}/ambient-{timestamp}.wav"
    save_wav(piece, wav_file)

    # Convert to MP3
    mp3_file = wav_file.replace('.wav', '.mp3')
    subprocess.run(['ffmpeg', '-i', wav_file, '-ac', '1', '-ar', '44100',
                   '-b:a', '192k', mp3_file, '-y'],
                  capture_output=True)

    print(f"\nSaved:")
    print(f"  WAV: {wav_file}")
    print(f"  MP3: {mp3_file}")

    if play_after:
        print("Playing...")
        subprocess.run(['aplay', '-q', wav_file])

    return mp3_file


if __name__ == '__main__':
    main()
