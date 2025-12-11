import time
import random
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
# 增加 max_http_buffer_size 以允許圖片上傳
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
        self.host_sid = None
        self.map_data = []
        self.bombs = []
        self.explosions = []
        self.is_running = False
        self.winner = None
        self.winner_sid = None
        self.start_player_count = 2
        self.available_spawns = []
        self.generate_map()

    def generate_map(self):
        while True:
            self.map_data = [[0] * MAP_SIZE for _ in range(MAP_SIZE)]
            walkable_coords = [] 

            # 1. 佈置硬牆 (Hard Walls)
            for y in range(MAP_SIZE):
                for x in range(MAP_SIZE):
                    if x == 0 or x == MAP_SIZE-1 or y == 0 or y == MAP_SIZE-1:
                        self.map_data[y][x] = 1
                    elif random.random() < 0.15: 
                        # 這裡不再強制避開角落，因為出生點是隨機選的
                        self.map_data[y][x] = 1
                    else:
                        self.map_data[y][x] = 0
                        walkable_coords.append((x, y))

            # 2. 連通性檢查 (BFS)
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
            
            if len(visited) != len(walkable_coords):
                continue
            
            # 3. 佈置軟牆 (磚塊) - 先隨機鋪滿
            for y in range(MAP_SIZE):
                for x in range(MAP_SIZE):
                    if self.map_data[y][x] == 0:
                        if random.random() < 0.4:
                            self.map_data[y][x] = 2

            # === 4. 生成隨機且安全的出生點 (關鍵修改) ===
            self.available_spawns = [] # 清空舊的
            candidates = []

            # 找出所有適合當出生點的位置
            # 條件：本身不是硬牆，且四周至少有 2 個鄰居不是硬牆 (這樣才能清出直角)
            for y in range(1, MAP_SIZE-1):
                for x in range(1, MAP_SIZE-1):
                    if self.map_data[y][x] == 1: continue # 硬牆不能當出生點

                    non_hard_neighbors = 0
                    for dx, dy in [(0,1), (0,-1), (1,0), (-1,0)]:
                        if self.map_data[y+dy][x+dx] != 1:
                            non_hard_neighbors += 1
                    
                    if non_hard_neighbors >= 2:
                        candidates.append((x, y))
            
            # 如果候選點太少 (極端情況)，重新生成地圖
            if len(candidates) < 8: continue

            # 從候選點中隨機選出 8 個作為本局的出生點
            random.shuffle(candidates)
            chosen_spawns = candidates[:8] # 取前 8 個

            for cx, cy in chosen_spawns:
                # A. 確保出生點本身是空地
                self.map_data[cy][cx] = 0
                
                # B. 強制清理周圍的軟牆，確保有路可走
                # 這樣保證了只要不是硬牆的方向，磚塊都會被移除，形成通路
                for dx, dy in [(0,1), (0,-1), (1,0), (-1,0)]:
                    nx, ny = cx + dx, cy + dy
                    if self.map_data[ny][nx] == 2: # 如果是磚塊
                        self.map_data[ny][nx] = 0  # 變成空地
                
                # 將這個處理好的安全座標加入列表
                self.available_spawns.append((cx, cy))

            # 生成成功
            break

    def reset_round(self):
        self.is_running = False
        self.bombs = []
        self.explosions = []
        self.generate_map() # 這裡會重新生成 available_spawns
        
        if self.winner_sid and self.winner_sid in self.players:
            self.host_sid = self.winner_sid
        elif self.players:
            self.host_sid = random.choice(list(self.players.keys()))

        self.winner = None
        self.winner_sid = None
        
        # 複製一份出生點列表來發送 (避免 pop 影響原始資料，雖然這裡無所謂)
        spawn_pool = list(self.available_spawns)
        random.shuffle(spawn_pool)

        for sid, p in self.players.items():
            p['alive'] = True
            p['is_ready'] = (sid == self.host_sid)
            p['input_dir'] = None 
            p['is_moving'] = False
            p['face_dir'] = (1, 0) 
            
            # --- 分配隨機安全出生點 ---
            if spawn_pool:
                sx, sy = spawn_pool.pop()
                p['x'] = sx
                p['y'] = sy
                p['target_x'] = sx
                p['target_y'] = sy
            else:
                # 萬一出生點不夠 (超過8人)，隨機找個空位 (備用方案)
                while True:
                    rx, ry = random.randint(1, MAP_SIZE-2), random.randint(1, MAP_SIZE-2)
                    if self.map_data[ry][rx] == 0:
                        p['x'] = rx 
                        p['y'] = ry
                        p['target_x'] = rx
                        p['target_y'] = ry
                        break

    def add_player(self, sid, name, avatar=None):
        if not self.players:
            self.host_sid = sid
        
        # --- 分配隨機安全出生點 ---
        # 找出目前還沒被佔用的出生點
        occupied_positions = set()
        for p in self.players.values():
            occupied_positions.add((int(p['x']), int(p['y'])))
        
        spawn_x, spawn_y = -1, -1
        
        # 從 available_spawns 裡找一個沒人的
        random.shuffle(self.available_spawns) # 洗牌增加隨機性
        found = False
        for sx, sy in self.available_spawns:
            if (sx, sy) not in occupied_positions:
                spawn_x, spawn_y = sx, sy
                found = True
                break
        
        if not found:
            # 沒空位了，隨機找地圖空地
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
        
        # 嚴格整數檢查
        itx, ity = int(round(tx)), int(round(ty))
        if self.map_data[ity][itx] != 0: return False # 撞牆/磚
        
        for b in self.bombs:
            if b['x'] == itx and b['y'] == ity: return False
            
        for other_sid, other_p in self.players.items():
            if other_sid == sid: continue
            if not other_p['alive']: continue 
            # 檢查目標位置
            if int(round(other_p['target_x'])) == itx and int(round(other_p['target_y'])) == ity:
                return False
            # 額外檢查：如果對方正在移動，也要避開他目前的位置(避免穿模)
            if int(round(other_p['x'])) == itx and int(round(other_p['y'])) == ity:
                return False
        return True
    
    def get_current_bomb_limit(self):
        # 確保 start_player_count 至少是目前連線人數 (避免 reset 後變 0 的問題)
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
        
        # 取得所有活著的玩家 SID
        alive_sids = [sid for sid, p in self.players.items() if p['alive']]
        
        # 取得目前總連線玩家數 (包含死掉的幽靈)
        total_players = len(self.players)

        # 狀況 1: 平手 (大家同歸於盡)
        if len(alive_sids) == 0:
            self.winner = "無人生還"
            self.is_running = False
            return

        # 狀況 2: 只有一個人活著
        if len(alive_sids) == 1:
            # 關鍵修正：只要這場遊戲"曾經"是多人的 (start_player_count > 1)
            # 或者 現在房間裡還有其他人 (total_players > 1)，就代表不是自己在測試
            if self.start_player_count > 1 or total_players > 1:
                sid = alive_sids[0]
                self.winner = self.players[sid]['name']
                self.winner_sid = sid
                self.is_running = False
                return
        
        # 狀況 3: 所有人都斷線光了
        if total_players == 0:
            self.winner = "所有人都離開了"
            self.is_running = False
            return

    def update(self):
        current_time = time.time()
        
        # 1. 玩家移動 (微調)
        for sid, p in self.players.items():
            if not p['alive']: continue
            
            # 強制校正浮點數誤差 (如果非常接近整數，就吸附過去)
            if not p['is_moving']:
                p['x'] = float(int(round(p['x'])))
                p['y'] = float(int(round(p['y'])))

            if p['is_moving']:
                dx = p['target_x'] - p['x']
                dy = p['target_y'] - p['y']
                dist = (dx**2 + dy**2) ** 0.5
                if dist <= PLAYER_SPEED:
                    p['x'] = float(p['target_x']) # 強制轉型確保一致
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

        # --- 2. 炸彈與連鎖爆炸 (優化版，防止死鎖) ---
        bombs_to_explode = []
        remaining_bombs_map = {} 
        for b in self.bombs:
            if current_time - b['timestamp'] > BOMB_TIMER:
                bombs_to_explode.append(b)
            else:
                remaining_bombs_map[(b['x'], b['y'])] = b

        processed_explosions = [] 
        
        # 安全機制：限制最大連鎖次數，防止 while True 卡死
        max_chain_loops = 100 
        loop_count = 0
        
        i = 0
        while i < len(bombs_to_explode):
            loop_count += 1
            if loop_count > max_chain_loops: break # 強制跳出

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
                            bombs_to_explode.append(chained_bomb) # 加入佇列
                            processed_explosions.append({'x': ex, 'y': ey, 'timestamp': current_time})
                            # 不 break，讓火光穿透這顆被誘爆的炸彈
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
        if request.sid in game.players:
            p = game.players[request.sid]
            sender_name = p['name']
            sender_avatar = p.get('avatar')
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
        # 確保人數足夠
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
            
            # === 強制更新初始人數 ===
            game.start_player_count = len(game.players)
            print(f"Game Started! Initial players: {game.start_player_count}") # Debug log
            # =====================
            
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
        
        # Check for winner
        if game.winner:
            print(f"Detected Winner: {game.winner}") 
            # We found a winner, break the loop to handle end-game sequence
            break
            
        if not game.is_running: break
        socketio.sleep(FRAME_TIME)
    
    # --- End Game Sequence ---
    if room_id in ROOMS:
        game = ROOMS[room_id]
        
        # 1. Capture the winner name explicitly by value
        winner_name = str(game.winner) 
        print(f"Broadcasting Game Over: {winner_name}")
        
        # 2. Emit the Game Over event
        socketio.emit('game_over_reset', {'winner': winner_name}, room=room_id)
        
        # 3. CRITICAL PAUSE: Wait for client to receive and process the event
        # Do NOT reset the game yet. Give the network time.
        socketio.sleep(1.0) 
        
        # 4. Now safely reset the game state
        game.reset_round()
        
        # 5. Finally, update the lobby for the next round
        broadcast_lobby_state(room_id, game)
if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)