# VPS Deployer Bot - Version 1 (Docker-based, systemd-enabled containers)
# Language: Python (discord.py)
# File: bot.py

"""
Features implemented in this file (Option A):
- Discord bot with admin-protected commands
- `!setadmin @user` to set admin (first run)
- `!createvps <ram_mb> <cpu_cores> <disk_gb> <os> @user <days>` creates a docker container configured to run systemd
  - Deploys an Ubuntu/Debian container using /sbin/init
  - Uses --privileged, mounts /sys/fs/cgroup, tmpfs for /run & /run/lock so `systemctl` works
  - Installs openssh-server and tmate inside the container and starts them
  - Creates a detached tmate session and captures the SSH access string to DM the user
  - Limits memory and CPU via Docker flags
  - NOTE: Disk sizing inside Docker is environment-dependent and may not be enforceable on all systems.
- `!suspendvps <vps_id>` pauses the container and notifies the owner
- `!regeneratessh <vps_id>` kills existing tmate server and creates a new session, DMs the owner
- Background task checks expiration and stops & removes expired containers, informs owners

REQUIREMENTS (host machine):
- Docker installed and running
- Bot must run with a user that can run `docker` commands (recommended: system user in `docker` group or root)
- Host must allow --privileged containers (this has security implications)

USAGE: python bot.py

"""

import discord
from discord.ext import commands, tasks
import asyncio
import subprocess
import shlex
import uuid
import json
import os
from datetime import datetime, timedelta

# ---------- Configuration ----------
TOKEN = "MTQ0MDY3Njk3ODE2NzUxNzI1NA.GvtrU2.xJYNj0uDLYJUAXvTHAI5nSrxusOeEJneuiYpng"
DATA_FILE = "vps_data.json"

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# admin_id will be set via !setadmin on first run
admin_id = None
vps_data = {}  # vps_id -> dict

# ---------- Helpers ----------

def save_data():
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump({k: v for k, v in vps_data.items()}, f, default=str)
    except Exception as e:
        print("Failed to save data:", e)


def load_data():
    global vps_data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                raw = json.load(f)
                # convert expires back to datetime
                for k, v in raw.items():
                    v['expires'] = datetime.fromisoformat(v['expires'])
                vps_data = raw
        except Exception as e:
            print("Failed to load data:", e)


async def run_cmd(cmd, timeout=300):
    """Run a shell command and return stdout, raises on non-zero exit."""
    print("RUN:", cmd)
    proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    text = out.decode(errors='ignore') if out else ''
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed (code {proc.returncode}): {text}")
    return text


def short_id():
    return uuid.uuid4().hex[:8]

# ---------- Docker utility functions ----------

async def create_systemd_container(vps_id, ram_mb, cpu_cores, disk_gb, os_name):
    # Choose image
    if os_name.lower().startswith('ubuntu'):
        image = 'ubuntu:22.04'
    else:
        image = 'debian:12'

    name = f'vps_{vps_id}'

    memory_flag = f"--memory={ram_mb}m"
    cpu_flag = f"--cpus={cpu_cores}"

    # NOTE: disk sizing is not generally enforced by Docker unless storage driver supports it.
    # We'll create a named volume (no guaranteed size) so user data persists if desired.
    volume_name = f'vol_{vps_id}'

    # Pull image
    await run_cmd(f'docker pull {image}')
    # create volume
    await run_cmd(f'docker volume create {volume_name}')

    # Run container with systemd as PID 1
    cmd = (
        f'docker run -d --privileged --name {name} {memory_flag} {cpu_flag} '
        f'-v {volume_name}:/var/lib/vpsdata '
        f'--tmpfs /run --tmpfs /run/lock -v /sys/fs/cgroup:/sys/fs/cgroup:ro '
        f'{image} /sbin/init'
    )
    container_id = (await run_cmd(cmd)).strip()
    return name, container_id


async def setup_inside_container(name):
    # Update + install openssh-server and tmate
    # Some minimal steps to ensure systemd can manage services
    cmds = [
        f'docker exec {name} apt-get update -y',
        f'docker exec {name} apt-get install -y --no-install-recommends systemd openssh-server tmate wget ca-certificates gnupg lsb-release',
        # ensure /var/run/sshd exists
        f'docker exec {name} mkdir -p /var/run/sshd',
        # set root password to something random (not recommended) or create 'vpsuser'
        f"docker exec {name} useradd -m -s /bin/bash vpsuser || true",
        f"docker exec {name} bash -c 'echo "vpsuser:password" | chpasswd' || true",
        # enable and start ssh
        f'docker exec {name} systemctl enable ssh || true',
        f'docker exec {name} systemctl start ssh || true',
    ]

    # execute sequentially and collect output for debugging
    for c in cmds:
        try:
            await run_cmd(c, timeout=600)
        except Exception as e:
            print('Warning: command failed:', c, e)


async def create_tmate_session(name):
    # Kill any existing tmate server to avoid stale sessions
    try:
        await run_cmd(f'docker exec {name} pkill -f tmate || true')
    except Exception:
        pass

    # Start tmate in detached mode
    # Create a socket path and start session
    start_cmd = f"docker exec -d {name} tmate -S /tmp/tmate.sock new-session -d"
    await run_cmd(start_cmd)

    # Wait a bit for session to be ready, then read the ssh string
    for _ in range(12):
        try:
            out = await run_cmd(f"docker exec {name} tmate -S /tmp/tmate.sock display -p '#{{tmate_ssh}}'", timeout=10)
            out = out.strip()
            if out:
                return out
        except Exception:
            await asyncio.sleep(1)
    raise RuntimeError('Failed to obtain tmate SSH string')


async def pause_container(name):
    await run_cmd(f'docker pause {name}')


async def unpause_container(name):
    await run_cmd(f'docker unpause {name}')


async def stop_and_remove_container(name, remove_volumes=False):
    try:
        await run_cmd(f'docker stop {name}')
    except Exception:
        pass
    try:
        await run_cmd(f'docker rm {name}')
    except Exception:
        pass

# ---------- Discord commands ----------

@bot.event
async def on_ready():
    global admin_id
    print('Bot ready:', bot.user)
    load_data()
    check_expired_vps.start()


@bot.command()
async def setadmin(ctx, user: discord.Member):
    global admin_id
    if admin_id:
        return await ctx.send('Admin already set.')
    admin_id = user.id
    await ctx.send(f'Admin set to {user.display_name}')


def is_admin_check(ctx):
    return ctx.author.id == admin_id


@bot.command()
async def createvps(ctx, ram_mb: int, cpu_cores: int, disk_gb: int, os_name: str, member: discord.Member, days: int):
    if admin_id is None:
        return await ctx.send('Admin not set. Use !setadmin @user first.')
    if ctx.author.id != admin_id:
        return

    if os_name.lower() not in ['ubuntu', 'debian']:
        return await ctx.send('OS must be Ubuntu or Debian')

    vps_id = short_id()
    await ctx.send(f'Creating VPS {vps_id}... (this may take some time)')

    try:
        name, container_id = await create_systemd_container(vps_id, ram_mb, cpu_cores, disk_gb, os_name)
        await setup_inside_container(name)
        tmate_ssh = await create_tmate_session(name)

        expire_dt = datetime.utcnow() + timedelta(days=days)

        vps_data[vps_id] = {
            'owner': member.id,
            'container_name': name,
            'container_id': container_id,
            'ram_mb': ram_mb,
            'cpu': cpu_cores,
            'disk_gb': disk_gb,
            'os': os_name,
            'created_at': datetime.utcnow().isoformat(),
            'expires': expire_dt,
            'suspended': False,
            'tmate_ssh': tmate_ssh,
        }
        save_data()

        ram_gb = ram_mb / 1024

        embed = discord.Embed(title='⭐ VPS Created!', description='Your ZothyNodes VPS has been successfully deployed!', color=0x00ff00)
        embed.add_field(name='VPS ID', value=vps_id)
        embed.add_field(name='OS', value=os_name)
        embed.add_field(name='RAM', value=f"{ram_gb:.2f} GB")
        embed.add_field(name='CPU', value=str(cpu_cores))
        embed.add_field(name='Time', value=f"{days} days")
        embed.add_field(name='Storage', value=f"{disk_gb} GB")

        await ctx.send(embed=embed)

        owner = bot.get_user(member.id)
        if owner:
            await owner.send(f"Your ZothyNodes VPS is ready!
VPS ID: {vps_id}
Tmate SSH:
```
{tmate_ssh}
```")

    except Exception as e:
        await ctx.send(f'Failed to create VPS: {e}')


@bot.command()
async def suspendvps(ctx, vps_id: str):
    if ctx.author.id != admin_id:
        return
    if vps_id not in vps_data:
        return await ctx.send('Invalid VPS ID')

    data = vps_data[vps_id]
    name = data['container_name']
    try:
        await pause_container(name)
        data['suspended'] = True
        save_data()
        owner = bot.get_user(data['owner'])
        if owner:
            await owner.send(f'Your VPS [{vps_id}] Has Been Suspended By Admin/Owner')
        await ctx.send(f'VPS {vps_id} suspended.')
    except Exception as e:
        await ctx.send(f'Failed to suspend VPS: {e}')


@bot.command()
async def regeneratessh(ctx, vps_id: str):
    if ctx.author.id != admin_id:
        return
    if vps_id not in vps_data:
        return await ctx.send('Invalid VPS ID')

    data = vps_data[vps_id]
    name = data['container_name']
    try:
        # kill any tmate server and produce a new SSH string
        await run_cmd(f'docker exec {name} pkill -f tmate || true')
        tmate_ssh = await create_tmate_session(name)
        data['tmate_ssh'] = tmate_ssh
        save_data()
        owner = bot.get_user(data['owner'])
        if owner:
            await owner.send(f'Your new SSH for VPS [{vps_id}]:
```
{tmate_ssh}
```')
        await ctx.send(f'SSH regenerated for VPS {vps_id}.')
    except Exception as e:
        await ctx.send(f'Failed to regenerate SSH: {e}')


# ---------- Background expiration checker ----------
@tasks.loop(minutes=1)
async def check_expired_vps():
    now = datetime.utcnow()
    for vps_id, data in list(vps_data.items()):
        expires = data['expires']
        if isinstance(expires, str):
            expires = datetime.fromisoformat(expires)
        if now >= expires:
            name = data['container_name']
            owner = bot.get_user(data['owner'])
            try:
                await stop_and_remove_container(name)
            except Exception as e:
                print('Error removing container', e)
            if owner:
                try:
                    await owner.send('Your Vps Time Has Reached So We Has Stopped Your Vps Service Please Contact Admin/Owner For Restart.')
                except Exception:
                    pass
            del vps_data[vps_id]
            save_data()


# ---------- Run ----------
if __name__ == '__main__':
    if TOKEN == 'YOUR_BOT_TOKEN_HERE':
        print('Please set your bot token in the TOKEN variable before running.')
    else:
        bot.run(TOKEN)
