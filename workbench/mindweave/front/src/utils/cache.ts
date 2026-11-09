const PREFIX = 'mw.cache.'
const DEFAULT_TTL = 60_000

export function readCache<T>(key: string): T | null {
  try {
    const raw = sessionStorage.getItem(PREFIX + key)
    if (!raw) return null
    const parsed = JSON.parse(raw) as { value: T; expiresAt: number }
    if (Date.now() > parsed.expiresAt) {
      sessionStorage.removeItem(PREFIX + key)
      return null
    }
    return parsed.value
  } catch {
    return null
  }
}

export function writeCache<T>(key: string, value: T, ttlMs: number = DEFAULT_TTL): void {
  try {
    sessionStorage.setItem(PREFIX + key, JSON.stringify({ value, expiresAt: Date.now() + ttlMs }))
  } catch {
    // 存储满或隐私模式：忽略，不影响主流程
  }
}
