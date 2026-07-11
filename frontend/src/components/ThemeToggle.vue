<template>
  <button
    class="theme-toggle"
    type="button"
    role="switch"
    :aria-checked="theme === 'light'"
    :aria-label="theme === 'light' ? '切换为深色模式' : '切换为浅色模式'"
    @click="$emit('toggle')"
  >
    <i class="fa-regular fa-sun theme-toggle-icon theme-toggle-sun" aria-hidden="true"></i>
    <i class="fa-regular fa-moon theme-toggle-icon theme-toggle-moon" aria-hidden="true"></i>
    <span class="theme-toggle-thumb" aria-hidden="true"></span>
  </button>
</template>

<script setup lang="ts">
defineProps<{
  theme: 'dark' | 'light';
}>();

defineEmits<{
  (e: 'toggle'): void;
}>();
</script>
