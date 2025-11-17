const defaultMediaInfo = {
  state: 'STOPPED',
  player_name: '',
  title: '',
  artist: '',
  album: '',
  cover_url: '',
  duration: '0:00',
  duration_seconds: 0,
  position: '0:00',
  position_seconds: 0,
  position_percent: 0,
  volume: 100,
  rating: 0,
  repeat_mode: 'NONE',
  shuffle_active: false,
  timestamp: 0
}

function registerSocket(_onMediaInfoChange) {
  function onMediaInfoChange(mediaInfo) {
    try { _onMediaInfoChange(mediaInfo) } catch {}
  }
  
  let ws = null
  let timeout = null
  open()
  onMediaInfoChange(defaultMediaInfo)

  function retry() {
    clearTimeout(timeout)
    onMediaInfoChange(defaultMediaInfo)
    try {
      ws.onclose = null
      ws.onerror = null
      ws.close()
    } catch {}
    ws = null
    open()
  }

  function open() {
    ws = new WebSocket('ws://localhost:6534')
    timeout = setTimeout(() => {
      // Retry if connection is not established after 5 seconds
      // and the websocket still hasn't errored/closed
      if (ws.readyState !== WebSocket.OPEN) retry()
    }, 5000)
    ws.onopen = () => ws.send('RECIPIENT')
    ws.onclose = () => retry()
    ws.onerror = () => retry()
    ws.onmessage = (e) => {
      try {
        const mediaInfo = mapJsonKeys(e.data)
        if (mediaInfo) {
          onMediaInfoChange(mediaInfo)
        }
      } catch {}
    }
  }
}

// Maps keys from pywnp < 2.0.0 to pywnp > 2.0.0
// Example: Player -> player_name
const LEGACY_KEY_MAP = {
  State: 'state',
  Player: 'player_name',
  PlayerName: 'player_name',
  player: 'player_name',
  Title: 'title',
  Artist: 'artist',
  Album: 'album',
  CoverUrl: 'cover_url',
  Duration: 'duration',
  DurationSeconds: 'duration_seconds',
  Position: 'position',
  PositionSeconds: 'position_seconds',
  PositionPercent: 'position_percent',
  Volume: 'volume',
  Rating: 'rating',
  RepeatState: 'repeat_mode',
  Shuffle: 'shuffle_active',
  ShuffleActive: 'shuffle_active',
  Timestamp: 'timestamp'
}

function mapJsonKeys(payload) {
  if (payload == null) {
    return null
  }

  let data = payload
  if (typeof payload === 'string') {
    try {
      data = JSON.parse(payload)
    } catch {
      return null
    }
  }

  if (typeof data !== 'object' || data === null) {
    return null
  }

  const normalized = {}
  for (const [key, value] of Object.entries(data)) {
    const mappedKey = LEGACY_KEY_MAP[key] || key
    normalized[mappedKey] = value
  }
  return normalized
}
