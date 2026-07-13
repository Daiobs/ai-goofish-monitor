import { ref, computed } from 'vue'
import { wsService } from '@/services/websocket'

// Global State
const username = ref<string | null>(null)
const isLoggedIn = ref(false)
const isInitialized = ref(false)
let restorePromise: Promise<void> | null = null

export function useAuth() {
  const isAuthenticated = computed(() => isLoggedIn.value)

  function setAuthenticated(user: string) {
    username.value = user
    isLoggedIn.value = true
    wsService.start()
  }

  function clearAuthentication() {
    username.value = null
    isLoggedIn.value = false
    wsService.stop()
  }

  async function restoreSession(): Promise<void> {
    if (isInitialized.value) {
      return
    }
    if (restorePromise) {
      return restorePromise
    }

    restorePromise = (async () => {
      try {
        const response = await fetch('/auth/session', {
          credentials: 'same-origin',
          cache: 'no-store',
        })
        const session = response.ok ? await response.json() : null
        if (session?.authenticated === true && typeof session.username === 'string') {
          setAuthenticated(session.username)
        } else {
          clearAuthentication()
        }
      } catch (e) {
        console.error('Session restore error', e)
        clearAuthentication()
      } finally {
        isInitialized.value = true
        restorePromise = null
      }
    })()

    return restorePromise
  }

  async function login(user: string, pass: string): Promise<boolean> {
    try {
      const response = await fetch('/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ username: user, password: pass }),
      })

      if (response.ok) {
        const session = await response.json()
        setAuthenticated(session.username)
        isInitialized.value = true
        return true
      }
      clearAuthentication()
      return false
    } catch (e) {
      console.error('Login error', e)
      clearAuthentication()
      return false
    }
  }

  async function logout(): Promise<void> {
    try {
      await fetch('/auth/logout', {
        method: 'POST',
        credentials: 'same-origin',
      })
    } catch (e) {
      console.error('Logout error', e)
    } finally {
      clearAuthentication()
      isInitialized.value = true
      window.location.assign('/login')
    }
  }

  return {
    username,
    isAuthenticated,
    isInitialized,
    restoreSession,
    login,
    logout
  }
}
