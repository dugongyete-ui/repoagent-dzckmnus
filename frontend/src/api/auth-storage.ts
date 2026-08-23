const ACCESS_TOKEN_KEY = 'access_token'
const REFRESH_TOKEN_KEY = 'refresh_token'

const storage = (): Storage | null => {
  if (typeof window === 'undefined' || !window.localStorage) return null
  return window.localStorage
}

export function getStoredToken(): string | null {
  return storage()?.getItem(ACCESS_TOKEN_KEY) ?? null
}

export function getStoredRefreshToken(): string | null {
  return storage()?.getItem(REFRESH_TOKEN_KEY) ?? null
}

export function storeToken(token: string): void {
  storage()?.setItem(ACCESS_TOKEN_KEY, token)
}

export function storeRefreshToken(token: string): void {
  storage()?.setItem(REFRESH_TOKEN_KEY, token)
}

export function clearStoredTokens(): void {
  storage()?.removeItem(ACCESS_TOKEN_KEY)
  storage()?.removeItem(REFRESH_TOKEN_KEY)
}
