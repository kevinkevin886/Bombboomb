import time
import random
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*")

# --- 遊戲參數 ---
MAP_SIZE = 12
PLAYER_SPEED = 0.25
BOMB_TIMER = 3
EXPLOSION_DURATION = 1
MAX_BOMBS = 1  # 新增：預設每人同時只能放 1 顆炸彈
FPS = 30
FRAME_TIME = 1.0 / FPS

DEFAULT_ROOM = "GLOBAL_ARENA"
ROOMS = {}

class GameState:
    def __init__(self):
        self.players = {}
        self.host_sid = None
        self.map_data = []
        self.bombs = []
        self.explosions = []
        self.is_running = False
        self.winner = None
        self.winner_sid = None
        self.generate_map()

    def generate_map(self):
        self.map_data = [[0] * MAP_SIZE for _ in range(MAP_SIZE)]
        for y in range(MAP_SIZE):
            for x in range(MAP_SIZE):
                if x == 0 or x == MAP_SIZE-1 or y == 0 or y == MAP_SIZE-1:
                    self.map_data[y][x] = 1
                elif random.random() < 0.2:
                    self.map_data[y][x] = 1
    
    def reset_round(self):
        self.is_running = False
        self.bombs = []
        self.explosions = []
        self.generate_map()
        
        if self.winner_sid and self.winner_sid in self.players:
            self.host_sid = self.winner_sid
        if self.host_sid not in self.players and self.players:
            self.host_sid = random.choice(list(self.players.keys()))

        self.winner = None
        self.winner_sid = None

        for sid, p in self.players.items():
            p['alive'] = True
            p['is_ready'] = (sid == self.host_sid)
            p['input_dir'] = None 
            p['is_moving'] = False
            p['face_dir'] = (1, 0) 
            
            while True:
                rx, ry = random.randint(1, MAP_SIZE-2), random.randint(1, MAP_SIZE-2)
                if self.map_data[ry][rx] == 0:
                    p['x'] = rx 
                    p['y'] = ry
                    p['target_x'] = rx
                    p['target_y'] = ry
                    break

    def add_player(self, sid, name):
        if not self.players:
            self.host_sid = sid

        while True:
            rx, ry = random.randint(1, MAP_SIZE-2), random.randint(1, MAP_SIZE-2)
            if self.map_data[ry][rx] == 0:
                self.players[sid] = {
                    'name': name,
                    'x': rx,
                    'y': ry,
                    'target_x': rx,
                    'target_y': ry,
                    'is_moving': False,
                    'input_dir': None,
                    'face_dir': (1, 0),
                    'color': f'#{random.randint(0, 0xFFFFFF):06x}',
                    'alive': True,
                    'is_ready': (sid == self.host_sid)
                }
                break

    def remove_player(self, sid):
        if sid in self.players:
            del self.players[sid]
            if sid == self.host_sid:
                self.host_sid = None
                if self.players:
                    new_host = random.choice(list(self.players.keys()))
                    self.host_sid = new_host
                    self.players[new_host]['is_ready'] = True
            return True
        return False

    def is_walkable(self, tx, ty, sid=None):
        if tx < 0 or tx >= MAP_SIZE or ty < 0 or ty >= MAP_SIZE:
            return False
        if self.map_data[int(ty)][int(tx)] == 1:
            return False
        for b in self.bombs:
            if b['x'] == tx and b['y'] == ty:
                return False
        for other_sid, other_p in self.players.items():
            if other_sid == sid: continue
            if not other_p['alive']: continue 
            if other_p['target_x'] == tx and other_p['target_y'] == ty:
                return False
        return True

    # --- 修改重點：限制炸彈數量 ---
    def place_bomb(self, sid):
        if sid not in self.players or not self.players[sid]['alive']: return
        
        # 1. 計算該玩家目前場上有幾顆未爆炸的炸彈
        current_bombs = 0
        for b in self.bombs:
            if b['owner'] == sid:
                current_bombs += 1
        
        # 2. 如果達到上限 (預設 1)，則禁止放置
        if current_bombs >= MAX_BOMBS:
            return

        p = self.players[sid]
        bx, by = int(round(p['x'])), int(round(p['y']))
        
        # 檢查該位置是否已有炸彈 (避免重複放置)
        for b in self.bombs:
            if b['x'] == bx and b['y'] == by: return
            
        self.bombs.append({'x': bx, 'y': by, 'owner': sid, 'timestamp': time.time()})

    def check_win(self):
        if not self.is_running: return
        alive_sids = [sid for sid, p in self.players.items() if p['alive']]
        if len(self.players) == 0:
            self.winner = "所有人都離開了"
            self.is_running = False
        elif len(alive_sids) == 1:
            sid = alive_sids[0]
            self.winner = self.players[sid]['name']
            self.winner_sid = sid
            self.is_running = False
        elif len(alive_sids) == 0:
            self.winner = "無人生還"
            self.is_running = False

    def update(self):
        current_time = time.time()
        
        for sid, p in self.players.items():
            if not p['alive']: continue

            if p['is_moving']:
                dx = p['target_x'] - p['x']
                dy = p['target_y'] - p['y']
                dist = (dx**2 + dy**2) ** 0.5
                
                if dist <= PLAYER_SPEED:
                    p['x'] = p['target_x']
                    p['y'] = p['target_y']
                    p['is_moving'] = False
                    
                    if p['input_dir']:
                        idx, idy = p['input_dir']
                        next_tx = int(p['x'] + idx)
                        next_ty = int(p['y'] + idy)
                        if self.is_walkable(next_tx, next_ty, sid):
                            p['target_x'] = next_tx
                            p['target_y'] = next_ty
                            p['is_moving'] = True
                            p['face_dir'] = p['input_dir']
                else:
                    move_x = (dx / dist) * PLAYER_SPEED
                    move_y = (dy / dist) * PLAYER_SPEED
                    p['x'] += move_x
                    p['y'] += move_y

            elif p['input_dir']:
                idx, idy = p['input_dir']
                next_tx = int(p['x'] + idx)
                next_ty = int(p['y'] + idy)
                if self.is_walkable(next_tx, next_ty, sid):
                    p['target_x'] = next_tx
                    p['target_y'] = next_ty
                    p['is_moving'] = True
                    p['face_dir'] = p['input_dir']

        # 炸彈處理
        new_bombs = []
        for b in self.bombs:
            # 如果時間到了，產生爆炸，且該炸彈不會被加入 new_bombs
            # 這意味著它從 self.bombs 移除了 -> 玩家的炸彈計數器 -1 -> 可以再放了
            if current_time - b['timestamp'] > BOMB_TIMER:
                range_len = MAP_SIZE 
                explodes = [{'x': b['x'], 'y': b['y']}]
                directions = [(0,1), (0,-1), (1,0), (-1,0)]
                for dx, dy in directions:
                    for i in range(1, range_len + 1):
                        ex, ey = b['x'] + dx * i, b['y'] + dy * i
                        if 0 <= ex < MAP_SIZE and 0 <= ey < MAP_SIZE:
                            if self.map_data[ey][ex] == 1: break
                            explodes.append({'x': ex, 'y': ey})
                        else: break 
                for exp in explodes:
                    self.explosions.append({'x': exp['x'], 'y': exp['y'], 'timestamp': current_time})
            else:
                new_bombs.append(b)
        self.bombs = new_bombs

        active_explosions = []
        for exp in self.explosions:
            if current_time - exp['timestamp'] < EXPLOSION_DURATION:
                active_explosions.append(exp)
                for sid, p in self.players.items():
                    if p['alive']:
                        px, py = int(round(p['x'])), int(round(p['y'] ))
                        if px == exp['x'] and py == exp['y']: 
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

def broadcast_lobby_state(room, game):
    lobby_data = []
    for sid, p in game.players.items():
        lobby_data.append({
            'name': p['name'],
            'is_host': (sid == game.host_sid),
            'is_ready': p['is_ready']
        })
    emit('update_lobby', {
        'players': lobby_data, 
        'is_running': game.is_running,
        'host_sid': game.host_sid
    }, room=room)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('req_lobby_update')
def on_req_lobby_update():
    room = DEFAULT_ROOM
    if room in ROOMS:
        broadcast_lobby_state(room, ROOMS[room])

@socketio.on('disconnect')
def on_disconnect():
    room = DEFAULT_ROOM
    if room in ROOMS:
        game = ROOMS[room]
        if game.remove_player(request.sid):
            broadcast_lobby_state(room, game)
            if game.is_running: game.check_win()

@socketio.on('create_join')
def on_join(data):
    name = data['name']
    room = DEFAULT_ROOM 
    join_room(room)
    if room not in ROOMS: ROOMS[room] = GameState()
    game = ROOMS[room]
    if game.is_running:
        emit('spectator_mode', {'msg': '遊戲進行中，您已進入觀戰模式'}, to=request.sid)
    else:
        if len(game.players) >= 8:
            emit('error', {'msg': '房間已滿'})
            return
        game.add_player(request.sid, name)
        broadcast_lobby_state(room, game)

@socketio.on('toggle_ready')
def on_toggle_ready():
    room = DEFAULT_ROOM
    if room in ROOMS:
        game = ROOMS[room]
        if request.sid in game.players:
            game.players[request.sid]['is_ready'] = not game.players[request.sid]['is_ready']
            broadcast_lobby_state(room, game)

@socketio.on('start_game')
def on_start(data):
    room = DEFAULT_ROOM
    if room in ROOMS:
        game = ROOMS[room]
        if request.sid != game.host_sid: return
        if len(game.players) < 2:
            emit('error', {'msg': '至少需要兩位玩家！'}, to=request.sid)
            return
        not_ready = [p['name'] for p in game.players.values() if not p['is_ready']]
        if not_ready:
            emit('error', {'msg': '等待其他玩家準備'}, to=request.sid)
            return
        if not game.is_running: 
            game.is_running = True
            game.winner = None
            socketio.start_background_task(game_loop, room)

@socketio.on('key_down')
def on_key_down(data):
    room = DEFAULT_ROOM
    if room in ROOMS:
        game = ROOMS[room]
        if request.sid not in game.players: return
        key = data['key']
        p = game.players[request.sid]
        
        if key == ' ':
            game.place_bomb(request.sid)
        else:
            if key in ['ArrowUp', 'w']: p['input_dir'] = (0, -1)
            elif key in ['ArrowDown', 's']: p['input_dir'] = (0, 1)
            elif key in ['ArrowLeft', 'a']: p['input_dir'] = (-1, 0)
            elif key in ['ArrowRight', 'd']: p['input_dir'] = (1, 0)

@socketio.on('key_up')
def on_key_up(data):
    room = DEFAULT_ROOM
    if room in ROOMS:
        game = ROOMS[room]
        if request.sid not in game.players: return
        key = data['key']
        p = game.players[request.sid]
        current_dir = p['input_dir']
        
        if current_dir:
            if (key in ['ArrowUp', 'w'] and current_dir == (0, -1)) or \
               (key in ['ArrowDown', 's'] and current_dir == (0, 1)) or \
               (key in ['ArrowLeft', 'a'] and current_dir == (-1, 0)) or \
               (key in ['ArrowRight', 'd'] and current_dir == (1, 0)):
                p['input_dir'] = None

@socketio.on('send_message')
def on_send_message(data):
    room = DEFAULT_ROOM
    if room in ROOMS:
        game = ROOMS[room]
        # 確認發話者是否在房間內
        if request.sid in game.players:
            sender_name = game.players[request.sid]['name']
            msg = data['msg']
            # 取得當前時間 HH:MM
            t = time.localtime()
            time_str = f"{t.tm_hour:02d}:{t.tm_min:02d}"
            
            # 廣播給所有人 (包含發送者)
            emit('new_message', {
                'name': sender_name,
                'msg': msg,
                'time': time_str,
                'sid': request.sid # 用來讓前端判斷是不是自己發的
            }, room=room)

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
    
    if room_id in ROOMS:
        game = ROOMS[room_id]
        socketio.emit('game_over_reset', {'winner': game.winner}, room=room_id)
        game.reset_round()
        socketio.sleep(0.1)
        broadcast_lobby_state(room_id, game)

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)