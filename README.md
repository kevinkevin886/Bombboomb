# 💣 Teams Bomberman

## 🚀 Installation & Execution

### 1\. Install Dependencies

Ensure you have Python installed, then run the following command in your terminal:

```bash
pip install flask flask-socketio eventlet
```

### 2\. Project Structure

Ensure your file structure looks like this:

```text
/your_project_folder
│
├── app.py                # Backend Core Logic
└── templates
    └── index.html        # Frontend UI & Game Logic
```

### 3\. Start the Server

Run the application from the project directory:

```bash
python app.py
```

### 4\. Play the Game

  * **Localhost**: Open your browser and visit `http://localhost:5000`
  * **LAN Play**: Find your local IP address (e.g., `192.168.1.10`). Other computers on the same network can join via `http://192.168.1.10:5000`.

## 🕹️ Controls

| Key | Action |
| :--- | :--- |
| **W / ↑** | Move Up |
| **S / ↓** | Move Down |
| **A / ←** | Move Left |
| **D / →** | Move Right |
| **Space** | Place Bomb |
| **Enter** | Send Chat Message |



**Have Fun\! 💣**