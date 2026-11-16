# zabbix-matrix-rain

[Русский](README.ru.md)

Matrix-style "digital rain" in your terminal, driven by your Zabbix. The busier
your hosts, the denser and faster the code falls; open problems tint a host's
band with the severity colour, and a new problem shakes the whole screen.

![demo mode](docs/demo.png)

Not very useful, but pretty: on a second monitor, a server room screen or as a
tmux screensaver.

## How the data becomes rain

Every host owns a band of screen columns.

| Zabbix data | Effect |
|---|---|
| CPU utilisation, % (`system.cpu.util`) | how many streams fall in the band and how fast |
| Interface traffic (`net.if.in[...]`, `net.if.out[...]`) | stream length |
| Open problems | part of the band takes the colour of the worst problem: yellow (warning), orange (average), red (high), magenta (disaster) |
| A new problem | the screen glitches, the host and problem flash in the centre and stay in the status line for 30 seconds |
| Now and then | a stream "decodes" and spells the host name, CPU load and problem count |

Problems already open at start-up show only as tinted bands: the glitch and the
banner fire for problems raised while the rain is running. Press `t` to see
what that looks like.

Hosts without fresh CPU data (e.g. network devices without such an item) stay
on screen, their band just falls slowly.

## Requirements

* Linux or macOS, a UTF-8 terminal with 256 colours (almost any will do);
* Python 3.8+, nothing else: standard library only;
* Zabbix 5.4+ with an API token (tested on 7.0).

On Windows run it in WSL.

## Quick start

```bash
git clone https://github.com/rozenpl/zabbix-matrix-rain.git
cd zabbix-matrix-rain
python3 zabbix_matrix_rain.py --demo
```

`--demo` shows simulated hosts and needs no Zabbix. Quit with `q` or `Esc`.

## Connecting to Zabbix

### 1. A read-only user

All it needs is a user that can see the hosts. In Zabbix 6.0+ (in 6.x the menu
items live under "Administration"):

1. **Users → User groups → Create**: e.g. `matrix-readonly`; on the "Host
   permissions" tab grant **Read** on the host groups you want to see.
2. **Users → Users → Create**: e.g. `matrix`, member of that group, role
   **User role**. Any long password will do, it is never used.
3. Optionally narrow the API: make your own role from User role and in "API
   methods" allow only `host.get`, `hostgroup.get`, `item.get`, `trigger.get`.
4. **Users → API tokens → Create API token** for this user. Copy the token:
   Zabbix shows it only once.

### 2. Put the token into a file

```bash
mkdir -p ~/.config
install -m 600 /dev/null ~/.config/zabbix-matrix-rain.token
nano ~/.config/zabbix-matrix-rain.token
```

The token is deliberately not accepted on the command line, where other users
of the machine could see it with `ps`.

### 3. Check and run

```bash
export ZABBIX_URL=https://zabbix.example.com
export ZABBIX_TOKEN_FILE=~/.config/zabbix-matrix-rain.token
python3 zabbix_matrix_rain.py --check
```

`--check` polls Zabbix once and prints a table of hosts, CPU, traffic and
problems. If it looks right:

```bash
python3 zabbix_matrix_rain.py
```

Certificate from a private CA: `--ca-file /path/to/ca.pem`. Self-signed:
`--insecure` (internal networks only).

## Keys

| Key | Action |
|---|---|
| `q`, `Esc` | quit |
| `h` | toggle the status line |
| `l` | toggle host names in the streams |
| `n` | spell every host name right now |
| `t` | test problem: glitch, banner and tint for a random host; severity rises with every press from warning to disaster. Nothing is sent to Zabbix |
| space | pause |

## Options

| Option | Environment | Meaning |
|---|---|---|
| `--url` | `ZABBIX_URL` | Zabbix frontend URL |
| `--token-file` | `ZABBIX_TOKEN_FILE` | file with the API token (or the token itself in `ZABBIX_TOKEN`) |
| `--group NAME` | `ZABBIX_GROUPS` (comma separated) | only hosts of these groups; repeatable |
| `--min-severity N` | | ignore problems below N (0..5, default 2 = warning) |
| `--interval S` | | seconds between polls (default 30) |
| `--insecure`, `--ca-file` | `ZABBIX_INSECURE=1`, `ZABBIX_CA_FILE` | certificate checks |
| `--cpu-key`, `--net-key` | | item key regexes, if your templates differ |
| `--stale S` | | values older than this count as missing (default 900) |
| `--anonymize` | | show `host-01`, `host-02`... and hide problem texts: for screenshots and streams |
| `--charset` | | `katakana`, `ascii`, `binary`, `hex` |
| `--spacing 2` | | use every second column: airier, closer to the film |
| `--fps` | | frames per second (default 20) |
| `--label-every S` | | average seconds between a host spelling its name (default 30) |
| `--no-labels`, `--no-hud` | | no host names, no status line |
| `--screensaver` | | quit on any key |
| `--transparent` | | keep the terminal background instead of black |
| `--demo`, `--demo-hosts N` | | simulated hosts |
| `--check` | | poll once, print a table, exit |

Full list: `python3 zabbix_matrix_rain.py --help`.

## Troubleshooting

* **Boxes instead of glyphs.** The terminal font has no half-width katakana:
  use `--charset ascii` or install a CJK font (e.g. Noto Sans Mono CJK).
* **`NO CARRIER` in the status line.** Zabbix is unreachable or rejected the
  token; the text after the colon says why. The rain goes on with the last data.
* **Every band falls slowly.** Your templates do not collect `system.cpu.util`.
  Look at `--check` and pass a `--cpu-key` regex matching an item that holds CPU
  utilisation in percent.
* **Certificate verification fails.** `--ca-file` or `--insecure`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests replace the Zabbix API with canned answers and use no network.

## License

MIT, see [LICENSE](LICENSE).
