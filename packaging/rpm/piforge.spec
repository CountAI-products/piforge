Name:           piforge
Version:        %{piforge_version}
Release:        1%{?dist}
Summary:        Mass SD card installer for Raspberry Pi OS
License:        MIT
URL:            https://github.com/gokul-hastrophil/piforge
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

# Package names below are verified against Fedora's live repositories
# (dnf list) as of this writing. openSUSE uses different names for some
# of these (notably the WebKitGTK typelib and PyGObject packages) — if
# you're packaging for openSUSE, adjust Requires accordingly; the app
# code itself is identical, only these names need distro-specific
# translation. See CONTRIBUTING.md if you'd like to contribute a
# verified openSUSE spec variant.
Requires:       python3
Requires:       python3-gobject
Requires:       gobject-introspection
Requires:       webkit2gtk4.1
Requires:       util-linux
Requires:       coreutils
Requires:       xz
Requires:       openssl
Requires:       wpa_supplicant
Requires:       parted
Requires:       dosfstools
Requires:       e2fsprogs
Requires:       gnupg2
Requires:       jq
Requires:       curl
Requires:       polkit

%description
PiForge flashes Raspberry Pi OS to many SD cards in parallel with
hostname, user, password, Wi-Fi, SSH keys, static IP, and other
first-boot settings pre-configured, so every card boots straight to a
working login with no setup wizard.

Decompresses the OS image once and writes every selected card
concurrently from the page cache for near-linear speedup with card
count. Includes named config profiles, a flash history log, and
per-card cancel/retry.

%prep
%setup -q

%build
# Nothing to compile — pure Python, shell, and static web assets.

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}/opt/piforge
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_datadir}/applications
mkdir -p %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
mkdir -p %{buildroot}%{_datadir}/polkit-1/actions

install -m 0644 server.py %{buildroot}/opt/piforge/server.py
install -m 0644 index.html %{buildroot}/opt/piforge/index.html
install -m 0644 PRODUCTION.md %{buildroot}/opt/piforge/PRODUCTION.md
install -m 0644 dashboard-finalize.sh %{buildroot}/opt/piforge/dashboard-finalize.sh
install -m 0644 clone.py %{buildroot}/opt/piforge/clone.py
install -m 0644 cloning.py %{buildroot}/opt/piforge/cloning.py
install -m 0644 blockmap.py %{buildroot}/opt/piforge/blockmap.py
install -m 0644 share_image.py %{buildroot}/opt/piforge/share_image.py
install -m 0644 localimages.py %{buildroot}/opt/piforge/localimages.py
install -m 0755 firstrun_gen.py %{buildroot}/opt/piforge/firstrun_gen.py
install -m 0644 config.example.json %{buildroot}/opt/piforge/config.example.json
install -m 0644 profiles.example.json %{buildroot}/opt/piforge/profiles.example.json
install -m 0644 tailscale_config.example.json %{buildroot}/opt/piforge/tailscale_config.example.json
install -m 0755 flash-all.sh %{buildroot}/opt/piforge/flash-all.sh
install -m 0755 inject-config.sh %{buildroot}/opt/piforge/inject-config.sh
install -m 0755 check-requirements.sh %{buildroot}/opt/piforge/check-requirements.sh
install -m 0644 README.md %{buildroot}/opt/piforge/README.md
install -m 0644 LICENSE %{buildroot}/opt/piforge/LICENSE
install -m 0755 packaging/piforge-server-root %{buildroot}/opt/piforge/piforge-server-root

install -m 0755 packaging/piforge %{buildroot}%{_bindir}/piforge
install -m 0644 packaging/debian/piforge.desktop %{buildroot}%{_datadir}/applications/piforge.desktop
install -m 0644 packaging/piforge.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/piforge.svg
install -m 0644 packaging/debian/io.github.gokul-hastrophil.piforge.policy \
    %{buildroot}%{_datadir}/polkit-1/actions/io.github.gokul-hastrophil.piforge.policy

%files
/opt/piforge/
%{_bindir}/piforge
%{_datadir}/applications/piforge.desktop
%{_datadir}/icons/hicolor/scalable/apps/piforge.svg
%{_datadir}/polkit-1/actions/io.github.gokul-hastrophil.piforge.policy

%changelog
* Sun Jul 26 2026 PiForge contributors <noreply@example.invalid> - %{version}-1
- See https://github.com/gokul-hastrophil/piforge/releases for release notes
