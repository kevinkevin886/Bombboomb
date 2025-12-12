import time
import random
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*", max_http_buffer_size=5 * 1024 * 1024)

# --- 遊戲參數 ---
MAP_SIZE = 12
PLAYER_SPEED = 0.25
BOMB_TIMER = 3
EXPLOSION_DURATION = 1
INITIAL_BOMB_LIMIT = 2 
FPS = 30
FRAME_TIME = 1.0 / FPS

DEFAULT_ROOM = "GLOBAL_ARENA"
ROOMS = {}

class GameState:
    def __init__(self):
        self.players = {}
        self.waiting_list = {} # [新增] 等待清單：存儲觀戰者的資料
        self.host_sid = None
        self.map_data = []
        self.bombs = []
        self.explosions = []
        self.is_running = False
        self.winner = None
        self.winner_sid = None
        self.start_player_count = 2
        
        self.SPAWN_POINTS = [
            (1, 1),   (10, 10), 
            (10, 1),  (1, 10),  
            (5, 1),   (6, 10),  
            (1, 6),   (10, 5)   
        ]
        self.generate_map()

    def generate_map(self):
        while True:
            self.map_data = [[0] * MAP_SIZE for _ in range(MAP_SIZE)]
            reserved_zones = set()
            for sx, sy in self.SPAWN_POINTS:
                reserved_zones.add((sx, sy))
                for dx, dy in [(0,1), (0,-1), (1,0), (-1,0)]:
                    reserved_zones.add((sx + dx, sy + dy))

            walkable_coords = [] 
            for y in range(MAP_SIZE):
                for x in range(MAP_SIZE):
                    if x == 0 or x == MAP_SIZE-1 or y == 0 or y == MAP_SIZE-1:
                        self.map_data[y][x] = 1
                    elif (x, y) in reserved_zones:
                        self.map_data[y][x] = 0
                        walkable_coords.append((x, y))
                    elif random.random() < 0.2: 
                        self.map_data[y][x] = 1
                    else:
                        self.map_data[y][x] = 0
                        walkable_coords.append((x, y))

            if not walkable_coords: continue
            
            start_node = walkable_coords[0]
            visited = set()
            queue = [start_node]
            visited.add(start_node)
            while queue:
                cx, cy = queue.pop(0)
                for dx, dy in [(0,1), (0,-1), (1,0), (-1,0)]:
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < MAP_SIZE and 0 <= ny < MAP_SIZE:
                        if self.map_data[ny][nx] != 1 and (nx, ny) not in visited:
                            visited.add((nx, ny))
                            queue.append((nx, ny))
            
            if len(visited) != len(walkable_coords): continue
            
            for y in range(MAP_SIZE):
                for x in range(MAP_SIZE):
                    if self.map_data[y][x] == 0:
                        if random.random() < 0.85:
                            self.map_data[y][x] = 2

            for sx, sy in self.SPAWN_POINTS:
                self.map_data[sy][sx] = 0
                valid_neighbors = []
                for dx, dy in [(0,1), (0,-1), (1,0), (-1,0)]:
                    nx, ny = sx + dx, sy + dy
                    if 1 <= nx < MAP_SIZE-1 and 1 <= ny < MAP_SIZE-1:
                        if self.map_data[ny][nx] != 1:
                           valid_neighbors.append((nx, ny))
                random.shuffle(valid_neighbors)
                safe_spots = valid_neighbors[:2] 
                for safe_x, safe_y in safe_spots:
                    self.map_data[safe_y][safe_x] = 0
            break

    def reset_round(self):
        self.is_running = False
        self.bombs = []
        self.explosions = []
        self.generate_map()
        
        # --- [新增] 將等待區(觀戰)的玩家轉正 ---
        # 這裡不需要檢查是否滿人，因為我們下面會重新分配位置
        # 如果超過8人，add_player 邏輯會處理(雖然UI沒擋，但後端能跑)
        for sid, info in list(self.waiting_list.items()):
            # 只有當總人數還沒爆滿時才加入
            if len(self.players) < 20: # 設定一個寬鬆上限防止崩潰
                self.add_player(sid, info['name'], info['avatar'])
        
        # 清空等待區
        self.waiting_list.clear()
        # ------------------------------------

        if self.winner_sid and self.winner_sid in self.players:
            self.host_sid = self.winner_sid
        elif self.players:
            self.host_sid = random.choice(list(self.players.keys()))

        self.winner = None
        self.winner_sid = None
        
        # 重新分配位置
        player_sids = list(self.players.keys())
        random.shuffle(player_sids)

        for i, sid in enumerate(player_sids):
            p = self.players[sid]
            spawn_idx = i % len(self.SPAWN_POINTS)
            sx, sy = self.SPAWN_POINTS[spawn_idx]
            
            p['alive'] = True
            p['is_ready'] = (sid == self.host_sid)
            p['input_dir'] = None 
            p['is_moving'] = False
            p['face_dir'] = (1, 0) 
            p['x'] = sx
            p['y'] = sy
            p['target_x'] = sx
            p['target_y'] = sy

    def add_player(self, sid, name, avatar=None):
        if not self.players:
            self.host_sid = sid
        
        current_count = len(self.players)
        if current_count < len(self.SPAWN_POINTS):
            spawn_x, spawn_y = self.SPAWN_POINTS[current_count]
        else:
            while True:
                rx, ry = random.randint(1, MAP_SIZE-2), random.randint(1, MAP_SIZE-2)
                if self.map_data[ry][rx] == 0:
                    spawn_x, spawn_y = rx, ry
                    break

        self.players[sid] = {
            'name': name,
            'avatar': avatar,
            'x': spawn_x,
            'y': spawn_y,
            'target_x': spawn_x,
            'target_y': spawn_y,
            'is_moving': False,
            'input_dir': None,
            'face_dir': (1, 0),
            'color': f'#{random.randint(0, 0xFFFFFF):06x}',
            'alive': True,
            'is_ready': (sid == self.host_sid)
        }

    def remove_player(self, sid):
        # [新增] 也檢查是否在等待區
        if sid in self.waiting_list:
            del self.waiting_list[sid]
            return True # 回傳 True 讓外面更新 Lobby

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
        if tx < 0 or tx >= MAP_SIZE or ty < 0 or ty >= MAP_SIZE: return False
        
        itx, ity = int(round(tx)), int(round(ty))
        if self.map_data[ity][itx] != 0: return False 
        
        for b in self.bombs:
            if b['x'] == itx and b['y'] == ity: return False
            
        for other_sid, other_p in self.players.items():
            if other_sid == sid: continue
            if not other_p['alive']: continue 
            if int(round(other_p['target_x'])) == itx and int(round(other_p['target_y'])) == ity:
                return False
            if int(round(other_p['x'])) == itx and int(round(other_p['y'])) == ity:
                return False
        return True
    
    def get_current_bomb_limit(self):
        base_count = max(self.start_player_count, len(self.players))
        
        current_alive = sum(1 for p in self.players.values() if p['alive'])
        dead_count = base_count - current_alive
        if dead_count < 0: dead_count = 0
        
        return INITIAL_BOMB_LIMIT + dead_count

    def place_bomb(self, sid):
        if sid not in self.players or not self.players[sid]['alive']: return
        
        current_user_bombs = 0
        for b in self.bombs:
            if b['owner'] == sid:
                current_user_bombs += 1
        
        dynamic_limit = self.get_current_bomb_limit()
        if current_user_bombs >= dynamic_limit: return

        p = self.players[sid]
        bx, by = int(round(p['x'])), int(round(p['y']))
        for b in self.bombs:
            if b['x'] == bx and b['y'] == by: return
        self.bombs.append({'x': bx, 'y': by, 'owner': sid, 'timestamp': time.time()})
    
    def check_win(self):
        if not self.is_running: return
        
        alive_sids = [sid for sid, p in self.players.items() if p['alive']]
        total_players = len(self.players)

        if len(alive_sids) == 0:
            self.winner = "無人生還"
            self.is_running = False
            return

        if len(alive_sids) == 1:
            if self.start_player_count > 1 or total_players > 1:
                sid = alive_sids[0]
                self.winner = self.players[sid]['name']
                self.winner_sid = sid
                self.is_running = False
                return
        
        if total_players == 0:
            self.winner = "所有人都離開了"
            self.is_running = False
            return

    def update(self):
        current_time = time.time()
        
        for sid, p in self.players.items():
            if not p['alive']: continue
            
            if not p['is_moving']:
                p['x'] = float(int(round(p['x'])))
                p['y'] = float(int(round(p['y'])))

            if p['is_moving']:
                dx = p['target_x'] - p['x']
                dy = p['target_y'] - p['y']
                dist = (dx**2 + dy**2) ** 0.5
                if dist <= PLAYER_SPEED:
                    p['x'] = float(p['target_x']) 
                    p['y'] = float(p['target_y'])
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

        bombs_to_explode = []
        remaining_bombs_map = {} 
        for b in self.bombs:
            if current_time - b['timestamp'] > BOMB_TIMER:
                bombs_to_explode.append(b)
            else:
                remaining_bombs_map[(b['x'], b['y'])] = b

        processed_explosions = [] 
        max_chain_loops = 100 
        loop_count = 0
        i = 0
        while i < len(bombs_to_explode):
            loop_count += 1
            if loop_count > max_chain_loops: break 
            b = bombs_to_explode[i]
            i += 1
            processed_explosions.append({'x': b['x'], 'y': b['y'], 'timestamp': current_time})
            range_len = MAP_SIZE 
            directions = [(0,1), (0,-1), (1,0), (-1,0)]
            for dx, dy in directions:
                for dist in range(1, range_len + 1):
                    ex, ey = b['x'] + dx * dist, b['y'] + dy * dist
                    if 0 <= ex < MAP_SIZE and 0 <= ey < MAP_SIZE:
                        if self.map_data[ey][ex] == 1:
                            break 
                        elif self.map_data[ey][ex] == 2:
                            self.map_data[ey][ex] = 0 
                            processed_explosions.append({'x': ex, 'y': ey, 'timestamp': current_time})
                            break 
                        elif (ex, ey) in remaining_bombs_map:
                            chained_bomb = remaining_bombs_map.pop((ex, ey)) 
                            bombs_to_explode.append(chained_bomb) 
                            processed_explosions.append({'x': ex, 'y': ey, 'timestamp': current_time})
                        else:
                            processed_explosions.append({'x': ex, 'y': ey, 'timestamp': current_time})
                    else:
                        break 

        self.bombs = list(remaining_bombs_map.values()) 
        self.explosions.extend(processed_explosions) 

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
            'is_running': self.is_running,
            'bomb_limit': self.get_current_bomb_limit()
        }

def broadcast_lobby_state(room, game):
    lobby_data = []
    # 傳送正在等待中的玩家，讓他們也出現在列表(可選，或是在聊天室顯示)
    # 這裡我們主要傳送正式玩家
    for sid, p in game.players.items():
        lobby_data.append({
            'name': p['name'],
            'avatar': p.get('avatar'),
            'is_host': (sid == game.host_sid),
            'is_ready': p['is_ready']
        })
    socketio.emit('update_lobby', {
        'players': lobby_data, 
        'is_running': game.is_running,
        'host_sid': game.host_sid
    }, room=room)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('send_message')
def on_send_message(data):
    room = DEFAULT_ROOM
    if room in ROOMS:
        game = ROOMS[room]
        # [修改] 讓觀戰者也能發言
        sender_name = "Unknown"
        sender_avatar = None
        
        if request.sid in game.players:
            p = game.players[request.sid]
            sender_name = p['name']
            sender_avatar = p.get('avatar')
        elif request.sid in game.waiting_list:
            p = game.waiting_list[request.sid]
            sender_name = p['name'] + " (觀戰)"
            sender_avatar = p.get('avatar')
            
        if sender_name != "Unknown":
            msg = data['msg']
            t = time.localtime()
            time_str = f"{t.tm_hour:02d}:{t.tm_min:02d}"
            emit('new_message', {
                'name': sender_name,
                'avatar': sender_avatar,
                'msg': msg,
                'time': time_str,
                'sid': request.sid
            }, room=room)

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
    avatar = data.get('avatar') 
    room = DEFAULT_ROOM 
    join_room(room)
    if room not in ROOMS: ROOMS[room] = GameState()
    game = ROOMS[room]
    
    if game.is_running:
        # [修改] 加入等待區
        game.waiting_list[request.sid] = {'name': name, 'avatar': avatar}
        emit('spectator_mode', {'msg': '遊戲進行中，您已進入觀戰模式'}, to=request.sid)
    else:
        if len(game.players) >= 8:
            emit('error', {'msg': '房間已滿'})
            return
        game.add_player(request.sid, name, avatar)
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
            emit('error', {'msg': '至少需要兩位玩家才能開始！'}, to=request.sid)
            return
        not_ready = [p['name'] for p in game.players.values() if not p['is_ready']]
        if not_ready:
            emit('error', {'msg': '等待其他玩家準備'}, to=request.sid)
            return
        if not game.is_running: 
            game.is_running = True
            game.winner = None
            game.start_player_count = len(game.players)
            print(f"Game Started! Initial players: {game.start_player_count}") 
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

def game_loop(room_id):
    while True:
        if room_id not in ROOMS: break
        game = ROOMS[room_id]
        state = game.update()
        socketio.emit('state_update', state, room=room_id)
        
        if game.winner:
            print(f"Detected Winner: {game.winner}") 
            break
            
        if not game.is_running: break
        socketio.sleep(FRAME_TIME)
    
    if room_id in ROOMS:
        game = ROOMS[room_id]
        winner_name = str(game.winner) 
        print(f"Broadcasting Game Over: {winner_name}")
        socketio.emit('game_over_reset', {'winner': winner_name}, room=room_id)
        socketio.sleep(1.0) 
        game.reset_round()
        broadcast_lobby_state(room_id, game)
if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)