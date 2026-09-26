# PEER TO CODE
---
Peer to Code is a lightweight desktop text/code editor built with Python's
Tkinter GUI toolkit. Alongside standard editing features (syntax
highlighting for Python, Shell, C/C++, C#, Lua, and Rust, a file explorer,
theming, drag-and-drop file opening, and running scripts from within the
editor), it adds real-time collaboration: multiple people can connect to
the same session (client/server or peer-to-peer) and edit the same file
together, with changes synchronized live using operational transformation.


## INSTRUCTIONS
---
1. Make sure Python 3 is installed on your system.
2. Clone or download the repository:
       git clone https://github.com/asard6348/peer-to-code.git
       cd peer-to-code
3. (Optional but recommended) Install the optional dependency for
   drag-and-drop support:
       pip install tkinterdnd2
4. Run the program:
       python main.py
   Optional arguments:
       python main.py --config /path/to/config.json   (use a custom config file)
       python main.py /path/to/file_or_folder          (open a file/folder on start)
5. When the app opens, use the "Connect" screen to either host a new
   collaborative session or join an existing one (via server address or
   peer-to-peer), then start editing together.


## DEPENDENCIES
---
- Python 3.x
- Tkinter (usually included with Python; on Linux you may need to install
  it separately, e.g. `sudo apt install python3-tk`)
- tkinterdnd2 (optional - enables drag-and-drop file opening; the app runs
  fine without it, just without that feature)
  
## CREDITS
---
- Claude Sonnet 5
