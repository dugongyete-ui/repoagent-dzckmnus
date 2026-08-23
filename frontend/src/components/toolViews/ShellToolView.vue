<template>
  <div
    class="h-[36px] flex items-center px-3 w-full bg-[var(--background-gray-main)] border-b border-[var(--border-main)] rounded-t-[12px] shadow-[inset_0px_1px_0px_0px_#FFFFFF] dark:shadow-[inset_0px_1px_0px_0px_#FFFFFF30]">
    <div class="flex-1 flex items-center justify-center">
      <div class="max-w-[250px] truncate text-[var(--text-tertiary)] text-sm font-medium text-center">{{
        shellSessionId }}
      </div>
    </div>
  </div>
  <div class="flex-1 min-h-0 w-full overflow-y-auto">
    <div dir="ltr" data-orientation="horizontal" class="flex flex-col flex-1 min-h-0">
      <div data-state="active" data-orientation="horizontal" role="tabpanel"
        id="radix-:r5m:-content-setup" tabindex="0"
        class="py-2 focus-visible:outline-none data-[state=inactive]:hidden flex-1 font-mono text-sm leading-relaxed px-3 outline-none overflow-auto whitespace-pre-wrap break-all text-[var(--text-primary)] bg-[var(--background-gray-main)]"
        style="animation-duration: 0s;">
        <code>
          <template v-for="entry in shell" :key="`${entry.ps1}-${entry.command}`">
            <span class="text-[var(--function-success)]">{{ entry.ps1 }}</span><span> {{ entry.command }}</span>
            <span>{{ entry.output }}</span>
            <span>{{ '\n' }}</span>
          </template>
        </code>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, computed, watch, onUnmounted } from 'vue';
import { viewShellSession } from '@/api/agent';
import { ToolContent } from '@/types/message';
//import { showErrorToast } from '@/utils/toast';

const props = defineProps<{
  sessionId: string;
  toolContent: ToolContent;
  live: boolean;
}>();

defineExpose({
  loadContent: () => {
    loadShellContent();
  }
});

const shell = ref<Array<{ ps1?: string; command?: string; output?: string }>>([]);
const refreshTimer = ref<ReturnType<typeof setInterval> | null>(null);

// Get shellSessionId from toolContent
const shellSessionId = computed(() => {
  if (props.toolContent && props.toolContent.args.id) {
    return props.toolContent.args.id;
  }
  return '';
});

const updateShellContent = (console: any) => {
  if (!Array.isArray(console)) {
    shell.value = [];
    return;
  }
  shell.value = console.map((entry) => ({
    ps1: entry?.ps1 == null ? '' : String(entry.ps1),
    command: entry?.command == null ? '' : String(entry.command),
    output: entry?.output == null ? '' : String(entry.output),
  }));
}

// Function to load Shell session content
const loadShellContent = async () => {
  if (!props.live) {
    updateShellContent(props.toolContent.content?.console);
    return;
  }
  
  if (!shellSessionId.value) return;

  try {
    const response = await viewShellSession(props.sessionId, shellSessionId.value);
    updateShellContent(response.console);
  } catch (error) {
    console.error("Failed to load shell content:", error);
  }
};

// Start auto-refresh timer
const startAutoRefresh = () => {
  if (refreshTimer.value) {
    clearInterval(refreshTimer.value);
  }
  
  if (props.live && shellSessionId.value) {
    refreshTimer.value = setInterval(() => {
      loadShellContent();
    }, 5000);
  }
};

// Stop auto-refresh timer
const stopAutoRefresh = () => {
  if (refreshTimer.value) {
    clearInterval(refreshTimer.value);
    refreshTimer.value = null;
  }
};

watch(() => props.toolContent, () => {
  loadShellContent();
});

watch(() => props.toolContent.timestamp, () => {
  loadShellContent();
});

// Watch for live prop changes
watch(() => props.live, (live: boolean) => {
  if (live) {
    loadShellContent();
    startAutoRefresh();
  } else {
    stopAutoRefresh();
  }
});

// Load content and set up refresh timer when component is mounted
onMounted(() => {
  loadShellContent();
  startAutoRefresh();
});

// Clear timer when component is unmounted
onUnmounted(() => {
  stopAutoRefresh();
});
</script>
