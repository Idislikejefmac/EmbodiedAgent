# Moving the project off this laptop

This laptop is a company device that will be returned. `data/` will hold family biometrics (faceprints, voiceprints, enrollment media), session recordings, memory claims and the encrypted registry. All of it must be moved to Jeff's own machine and then securely deleted here before the laptop goes back. Do this well before the return date, and also take periodic backups.

## 1. Export (on the old machine)

```powershell
.\.venv\Scripts\python tools\migrate.py export D:\backup\embodied.eamig
```

Before relying on an archive, check it without unpacking: `python tools\migrate.py verify D:\backup\embodied.eamig` (decrypts it, checks its structure and size, and lists the members). Restore refuses a destination that already has files unless you add `--force`, in which case it prints each path it will replace first; restore also refuses any archive made with a key-derivation cost other than the one export writes, and any archive larger than the size limit. The passphrase must be at least 12 characters. The tool refuses to overwrite an existing archive (use `--force`), refuses an output path inside `data/`, and refuses symlinks in `data/` (replace them with real files first).

You choose a passphrase. The archive holds `data/`, `.env` and `config.toml`, encrypted with a key derived from the passphrase (scrypt, AES-256-GCM). Store the passphrase somewhere safe: without it the archive is unreadable.

The code and docs are already in the Git repository (`https://github.com/Idislikejefmac/EmbodiedAgent`), so they come across with a clone.

## 2. Restore (on the new machine)

```powershell
git clone https://github.com/Idislikejefmac/EmbodiedAgent.git
cd EmbodiedAgent
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python tools\migrate.py restore D:\backup\embodied.eamig --dest .
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m brain
```

Check that the brain starts and reports the same keys as before.

## 3. The registry encryption key

From card P3-01 the registry key lives in the Windows credential store of the machine that created it, not in `data/`. The archive does not contain it. Before returning the laptop either export the key to your own safe storage and import it on the new machine, or re-enroll people on the new machine (a few minutes each). This section is filled in properly when P3-01 is built.

## 4. Secure-delete checklist for the old machine

- [ ] Restore succeeded on the new machine, and the brain's tests and a startup check pass there.
- [ ] A second copy of the archive exists on separate storage.
- [ ] Delete `data/`, `.env` and the whole repo folder from this laptop.
- [ ] Empty the Recycle Bin.
- [ ] Overwrite the freed space: `cipher /w:C:\jeffsource` (takes a while). On an SSD this is not a reliable wipe, because the drive remaps blocks. The real protection is BitLocker disk encryption plus the company's own device wipe on return, so treat this step as a courtesy and rely on the next item for anything secret.
- [ ] Remove the registry key from the Windows credential store (Control Panel, Credential Manager, Windows Credentials) once the P3-01 key exists.
- [ ] Remove saved Git and GitHub credentials, and sign out of GitHub on this machine.
- [ ] Remove the phone pairing for wireless debugging from this machine (phone: Developer options, Wireless debugging, Paired devices, forget this computer).
- [ ] Remove any API keys that were stored in the environment or `.env`, and rotate the Anthropic key regardless, since an SSD may keep remnants.
