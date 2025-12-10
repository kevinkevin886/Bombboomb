import time
import random
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*")

# --- 遊戲參數設定 ---
MAP_SIZE = 12        # 修改點 1: 改為 12x12
PLAYER_SPEED = 0.15
PLAYER_SIZE = 0.7
BOMB_TIMER = 3
EXPLOSION_DURATION = 1
FPS = 30
FRAME_TIME = 1.0 / FPS

DEFAULT_ROOM = "GLOBAL_ARENA" # 修改點 2: 固定的房間名稱
ROOMS = {}

class GameState:
    def __init__(self):
        self.players = {}  
        self.map_data = [[0] * MAP_SIZE for _ in range(MAP_SIZE)]
        self.bombs = []
        self.explosions = []
        self.is_running = False
        self.winner = None
        self.generate_map()

    def generate_map(self):
        for y in range(MAP_SIZE):
            for x in range(MAP_SIZE):
                if x == 0 or x == MAP_SIZE-1 or y == 0 or y == MAP_SIZE-1:
                    self.map_data[y][x] = 1
                elif random.random() < 0.2: # 障礙物密度
                    self.map_data[y][x] = 1
    
    def add_player(self, sid, name):
        while True:
            rx, ry = random.randint(1, MAP_SIZE-2), random.randint(1, MAP_SIZE-2)
            if self.map_data[ry][rx] == 0:
                self.players[sid] = {
                    'name': name,
                    'x': rx + (1 - PLAYER_SIZE) / 2,
                    'y': ry + (1 - PLAYER_SIZE) / 2,
                    'dx': 0, 'dy': 0,
                    'color': f'#{random.randint(0, 0xFFFFFF):06x}',
                    'alive': True
                }
                break

    def set_player_dir(self, sid, dx, dy):
        if sid in self.players and self.players[sid]['alive']:
            self.players[sid]['dx'] = dx
            self.players[sid]['dy'] = dy

    def check_collision(self, x, y, sid=None):
        corners = [
            (x, y), 
            (x + PLAYER_SIZE, y), 
            (x, y + PLAYER_SIZE), 
            (x + PLAYER_SIZE, y + PLAYER_SIZE)
        ]
        
        for cx, cy in corners:
            ix, iy = int(cx), int(cy)
            if ix < 0 or ix >= MAP_SIZE or iy < 0 or iy >= MAP_SIZE:
                return True
            if self.map_data[iy][ix] == 1:
                return True
            for b in self.bombs:
                bx, by = int(b['x']), int(b['y'])
                if bx == ix and by == iy:
                    if sid:
                        p = self.players[sid]
                        # 脫離炸彈邏輯
                        if (p['x'] < bx + 1 and p['x'] + PLAYER_SIZE > bx and
                            p['y'] < by + 1 and p['y'] + PLAYER_SIZE > by):
                            continue 
                    return True
        return False

    def place_bomb(self, sid):
        if sid not in self.players or not self.players[sid]['alive']: return
        p = self.players[sid]
        bx = int(p['x'] + PLAYER_SIZE / 2)
        by = int(p['y'] + PLAYER_SIZE / 2)
        for b in self.bombs:
            if b['x'] == bx and b['y'] == by: return
        self.bombs.append({'x': bx, 'y': by, 'owner': sid, 'timestamp': time.time()})

    def check_win(self):
        if not self.is_running: return
        alive = [p for p in self.players.values() if p['alive']]
        # 若遊戲開始且有人連線
        if len(self.players) > 0: 
            if len(self.players) > 1 and len(alive) == 1:
                self.winner = alive[0]['name']
                self.is_running = False
            elif len(alive) == 0:
                self.winner = "沒有人"
                self.is_running = False

    def update(self):
        current_time = time.time()
        
        # 移動
        for sid, p in self.players.items():
            if not p['alive']: continue
            new_x = p['x'] + p['dx'] * PLAYER_SPEED
            if not self.check_collision(new_x, p['y'], sid): p['x'] = new_x
            new_y = p['y'] + p['dy'] * PLAYER_SPEED
            if not self.check_collision(p['x'], new_y, sid): p['y'] = new_y

        # 炸彈邏輯
        new_bombs = []
        for b in self.bombs:
            if current_time - b['timestamp'] > BOMB_TIMER:
                # 修改點 3: 爆炸範圍設為 MAP_SIZE (全圖)
                range_len = MAP_SIZE 
                explodes = [{'x': b['x'], 'y': b['y']}]
                directions = [(0,1), (0,-1), (1,0), (-1,0)]
                for dx, dy in directions:
                    for i in range(1, range_len + 1):
                        ex, ey = b['x'] + dx * i, b['y'] + dy * i
                        # 邊界與牆壁檢查
                        if 0 <= ex < MAP_SIZE and 0 <= ey < MAP_SIZE:
                            if self.map_data[ey][ex] == 1: # 遇到牆壁停止 (不會炸穿牆)
                                break
                            explodes.append({'x': ex, 'y': ey})
                        else:
                            break 
                for exp in explodes:
                    self.explosions.append({'x': exp['x'], 'y': exp['y'], 'timestamp': current_time})
            else:
                new_bombs.append(b)
        self.bombs = new_bombs

        # 爆炸判定
        active_explosions = []
        for exp in self.explosions:
            if current_time - exp['timestamp'] < EXPLOSION_DURATION:
                active_explosions.append(exp)
                for sid, p in self.players.items():
                    if p['alive']:
                        cx = int(p['x'] + PLAYER_SIZE / 2)
                        cy = int(p['y'] + PLAYER_SIZE / 2)
                        if cx == exp['x'] and cy == exp['y']:
                            p['alive'] = False
        self.explosions = active_explosions
        self.check_win()

        return {
            'players': self.players,
            'map': self.map_data,
            'bombs': self.bombs,
            'explosions': self.explosions,
            'winner': self.winner,
            'is_running': self.is_running
        }

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('create_join')
def on_join(data):
    name = data['name']
    room = DEFAULT_ROOM # 強制使用預設房間
    join_room(room)
    
    if room not in ROOMS: ROOMS[room] = GameState()
    game = ROOMS[room]
    
    if len(game.players) >= 8 or game.is_running:
        emit('error', {'msg': '無法加入：房間已滿或遊戲進行中'})
        return

    game.add_player(request.sid, name)
    
    # 修改點 4: 加入成功後，通知房間內所有人更新玩家列表
    player_list = [p['name'] for p in game.players.values()]
    emit('update_lobby', {'players': player_list, 'is_running': game.is_running}, room=room)

@socketio.on('start_game')
def on_start(data):
    room = DEFAULT_ROOM
    if room in ROOMS:
        if not ROOMS[room].is_running:
            ROOMS[room].is_running = True
            ROOMS[room].winner = None
            socketio.start_background_task(game_loop, room)

@socketio.on('key_down')
def on_key_down(data):
    room = DEFAULT_ROOM
    key = data['key']
    if room in ROOMS:
        game = ROOMS[room]
        if key == 'ArrowUp' or key == 'w': game.set_player_dir(request.sid, 0, -1)
        elif key == 'ArrowDown' or key == 's': game.set_player_dir(request.sid, 0, 1)
        elif key == 'ArrowLeft' or key == 'a': game.set_player_dir(request.sid, -1, 0)
        elif key == 'ArrowRight' or key == 'd': game.set_player_dir(request.sid, 1, 0)
        elif key == ' ': game.place_bomb(request.sid)

@socketio.on('key_up')
def on_key_up(data):
    room = DEFAULT_ROOM
    key = data['key']
    if room in ROOMS:
        game = ROOMS[room]
        p = game.players.get(request.sid)
        if p:
            if (key in ['w', 'ArrowUp'] and p['dy'] == -1) or \
               (key in ['s', 'ArrowDown'] and p['dy'] == 1): p['dy'] = 0
            if (key in ['a', 'ArrowLeft'] and p['dx'] == -1) or \
               (key in ['d', 'ArrowRight'] and p['dx'] == 1): p['dx'] = 0

def game_loop(room_id):
    while True:
        if room_id not in ROOMS: break
        game = ROOMS[room_id]
        state = game.update()
        socketio.emit('state_update', state, room=room_id)
        if game.winner:
            socketio.sleep(0.1)
            break
        if not game.is_running: break
        socketio.sleep(FRAME_TIME)

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)