# Peer to Code

A lightweight text and code editor with real-time collaboration.
Edit the same file together with other people, live, over a server or peer-to-peer.

## Features

- **Live collaboration** – host or join a session and edit together, with
  everyone's cursors visible
- **Two ways to connect** – a host/client session, or a decentralized
  peer-to-peer mesh
- **Chat** – talk to your session without leaving the editor
- **Tabs** – work on several files at once
- **Syntax highlighting** for 30+ languages
- **Run your code** from the editor, with output in a built-in terminal
- **File explorer**, drag-and-drop file opening, and themes
- **Dockable panels** – move the Explorer, Terminal and Chat around, or
  detach them into their own windows

## Download

Grab the file for your OS from the
[Releases page](https://github.com/asard6348/peer-to-code/releases/latest)
and run it.

> Unsigned executables may trigger a SmartScreen or Gatekeeper warning.

## Run from source

You need Python 3 with Tkinter (on Linux: `sudo apt install python3-tk`).

```bash
git clone https://github.com/asard6348/peer-to-code.git
cd peer-to-code
pip install tkinterdnd2   # optional, enables drag-and-drop
python main.py
```

Optional arguments:

```bash
python main.py /path/to/file_or_folder     # open a file or folder on start
python main.py --config /path/to/config.json   # use a custom config file
```

## Getting started

When the app opens, pick a tab on the start screen:

- **Open** – just edit locally, no networking
- **Connect** – host a session, or join one by address (`host:port`)
- **Peer to Peer** – start a new mesh, or join one through any member

## License

[GPL-3.0](LICENSE.txt)

## Credits

- Code: Claude Sonnet
- Icon: [@le-jacob](https://github.com/le-jacob)
