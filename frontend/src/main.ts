import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import './assets/global.css'
import './assets/theme.css'
import 'highlight.js/styles/github-dark.css'
import './utils/toast'
import i18n from './composables/useI18n'
import { getStoredToken } from './api/auth'
import { getCachedClientConfig } from './api/config'
import { configure } from "vue-gtag"

// Keep the public landing page eager; load authenticated/share routes on demand
// so a heavy or optional tool view cannot prevent the initial app mount.
import LandingPage from './pages/LandingPage.vue'

// Create router
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      component: LandingPage
    },
    {
      path: '/chat',
      component: () => import('./pages/MainLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        {
          path: '',
          component: () => import('./pages/HomePage.vue'),
          alias: ['/home'],
          meta: { requiresAuth: true }
        },
        {
          path: ':sessionId',
          component: () => import('./pages/ChatPage.vue'),
          meta: { requiresAuth: true }
        }
      ]
    },
    {
      path: '/share',
      component: () => import('./pages/ShareLayout.vue'),
      children: [
        {
          path: ':sessionId',
          component: () => import('./pages/SharePage.vue'),
        }
      ]
    },
    {
      path: '/login',
      component: () => import('./pages/LoginPage.vue')
    }
  ]
})

// Global route guard
router.beforeEach(async (to, _, next) => {
  const requiresAuth = to.matched.some((record: any) => record.meta?.requiresAuth)
  const hasToken = !!getStoredToken()
  const clientConfig = await getCachedClientConfig()
  const authProvider = clientConfig?.auth_provider ?? null

  if (requiresAuth) {
    if (authProvider === 'none' || authProvider === null) {
      next()
      return
    }

    if (!hasToken) {
      next({
        path: '/login',
        query: { redirect: to.fullPath }
      })
      return
    }
  }

  if (to.path === '/login') {
    if (authProvider === 'none') {
      next('/chat')
      return
    }
    if (hasToken) {
      next('/chat')
      return
    }
  }

  // Redirect authenticated users away from landing page to chat
  if (to.path === '/') {
    if (authProvider === 'none' || hasToken) {
      next('/chat')
      return
    }
  }

  next()
})

// Preload client runtime config and initialize Google Analytics.
void getCachedClientConfig().then((config) => {
  if (config?.google_analytics_id) {
    configure({ tagId: config.google_analytics_id })
  }
})

const app = createApp(App)

app.use(router)
app.use(i18n)
app.mount('#app')
