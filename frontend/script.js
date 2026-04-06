const { createApp } = Vue;

createApp({
    data() {
        return {
            messages: [],
            userInput: '',
            isLoading: false,
            activeNav: 'newChat',
            API_URL: '/chat',
            abortController: null,
            userId: 'user_' + Math.random().toString(36).substring(2, 11),
            sessionId: 'session_' + Date.now(),
            sessions: [],
            showHistorySidebar: false,
            isComposing: false,
            // 文档管理相关
            documents: [],
            documentsLoading: false,
            selectedFile: null,
            isUploading: false,
            uploadProgress: '',
            showTooltip: false,
            tooltipText: '',
            tooltipX: 0,
            tooltipY: 0,
            // 个人档案相关
            userProfile: null,
            selectedProfileFile: null,
            isUploadingProfile: false,
            profileProgress: '',
            uploadMode: 'replace',
            showChartModal: false,
            showDictionaryModal: false,
            /** NCI 词典：直接调 glossary API（iframe 内嵌 widget 会先走 Adobe Analytics，易被拦截导致 Search 无反应） */
            nciDictionaryQuery: '',
            nciDictionaryResults: [],
            nciDictionaryTotal: 0,
            nciDictionaryLoading: false,
            nciDictionaryError: '',
            nciDictionarySearched: false,
            /** NCI API：Begins=前缀，Contains=包含 */
            nciDictionaryMatchType: 'Begins',
            selectedChartIndicator: '',
            chartInstance: null,
            // 提问优化
            optimizedQuestions: [],
            isOptimizing: false,
            // 思考模式
            thinkMode: 'normal'
        };
    },
    computed: {
        uniqueTestItemNames() {
            if (!this.userProfile || !this.userProfile.test_items) return [];
            const names = this.userProfile.test_items.map(item => item.item_name);
            return [...new Set(names)].filter(Boolean);
        }
    },
    mounted() {
        this.configureMarked();
        // 尝试从 localStorage 恢复用户ID
        const savedUserId = localStorage.getItem('userId');
        if (savedUserId) {
            this.userId = savedUserId;
        } else {
            localStorage.setItem('userId', this.userId);
        }
        this.loadProfile();
        
        // 事件委托处理点击引用跳转
        if (this.$refs.chatContainer) {
            this.$refs.chatContainer.addEventListener('click', (e) => {
                const citeRef = e.target.closest('.cite-ref');
                if (citeRef) {
                    const msgIndex = citeRef.getAttribute('data-msg-index');
                    const chunkIndex = citeRef.getAttribute('data-chunk-index');
                    if (msgIndex != null && chunkIndex != null) {
                        this.scrollToChunk(msgIndex, chunkIndex);
                    }
                }
            });
        }
        
        // 处理全局的名词解释气泡点击
        document.addEventListener('click', (e) => {
            const conceptRef = e.target.closest('.concept-tooltip');
            if (conceptRef) {
                const desc = conceptRef.getAttribute('data-desc');
                if (desc) {
                    this.tooltipText = desc;
                    this.showTooltip = true;
                    this.$nextTick(() => {
                        const tooltip = this.$refs.globalTooltip;
                        if (tooltip) {
                            const rect = conceptRef.getBoundingClientRect();
                            let left = rect.left + (rect.width / 2) - (tooltip.offsetWidth / 2);
                            let top = rect.top - tooltip.offsetHeight - 8;
                            
                            // 边界检测
                            if (left < 10) left = 10;
                            if (top < 10) top = rect.bottom + 8;
                            
                            this.tooltipX = left;
                            this.tooltipY = top;
                        }
                    });
                }
            } else {
                this.showTooltip = false;
            }
        });
    },
    methods: {
        configureMarked() {
            marked.setOptions({
                highlight: function(code, lang) {
                    const language = hljs.getLanguage(lang) ? lang : 'plaintext';
                    return hljs.highlight(code, { language }).value;
                },
                langPrefix: 'hljs language-',
                breaks: true,
                gfm: true
            });
        },
        
        parseMarkdown(text, msgIndex) {
            let html = marked.parse(text);
            let inCode = false;
            return html.split(/(<[^>]*>)/).map(part => {
                if (part.startsWith('<')) {
                    if (part.startsWith('<code') || part.startsWith('<pre')) inCode = true;
                    if (part.startsWith('</code') || part.startsWith('</pre')) inCode = false;
                    return part;
                }
                if (!inCode) {
                    return part.replace(/\[([\d\s,]+)\]/g, (match, p1) => {
                        const numbers = p1.split(',').map(n => n.trim()).filter(n => /^\d+$/.test(n));
                        if (numbers.length === 0) return match;
                        return numbers.map(n => `<sup class="cite-ref" data-msg-index="${msgIndex}" data-chunk-index="${n}">[${n}]</sup>`).join('');
                    });
                }
                return part;
            }).join('');
        },
        
        scrollToChunk(msgIndex, chunkIndex) {
            const msgEl = document.querySelectorAll('.message')[msgIndex];
            if (msgEl) {
                const details = msgEl.querySelector('details.references-details');
                if (details) {
                    details.open = true;
                }
                const chunkEl = document.getElementById(`chunk-${msgIndex}-${chunkIndex}`);
                if (chunkEl) {
                    chunkEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    chunkEl.classList.add('highlight-chunk');
                    setTimeout(() => {
                        chunkEl.classList.remove('highlight-chunk');
                    }, 2000);
                }
            }
        },

        async copyText(text, event) {
            const icon = event.currentTarget.querySelector('i');
            const oldClass = icon.className;
            
            const successCallback = () => {
                icon.className = 'fas fa-check';
                setTimeout(() => {
                    icon.className = oldClass;
                }, 2000);
            };

            try {
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(text);
                    successCallback();
                } else {
                    // Fallback for non-secure contexts (HTTP over network)
                    const textArea = document.createElement("textarea");
                    textArea.value = text;
                    textArea.style.position = "fixed";
                    textArea.style.left = "-999999px";
                    document.body.appendChild(textArea);
                    textArea.focus();
                    textArea.select();
                    document.execCommand('copy');
                    textArea.remove();
                    successCallback();
                }
            } catch (err) {
                console.error('Failed to copy text: ', err);
                alert("复制失败，您的浏览器不支持该功能。");
            }
        },

        toggleAction(event, type) {
            const icon = event.currentTarget.querySelector('i');
            if (type === 'up') {
                icon.className = icon.className.includes('fas') ? 'far fa-thumbs-up' : 'fas fa-thumbs-up';
            } else if (type === 'down') {
                icon.className = icon.className.includes('fas') ? 'far fa-thumbs-down' : 'fas fa-thumbs-down';
            } else if (type === 'share') {
                const oldClass = icon.className;
                icon.className = 'fas fa-check';
                setTimeout(() => { icon.className = oldClass; }, 1000);
            } else {
                // Default for star
                icon.className = icon.className.includes('fas') ? 'far fa-star' : 'fas fa-star';
            }
        },

        sendFollowUp(question) {
            this.userInput = question;
            this.handleSend();
        },

        openDictionaryModal() {
            this.nciDictionaryError = '';
            this.showDictionaryModal = true;
        },

        closeDictionaryModal() {
            this.showDictionaryModal = false;
        },

        async runNciDictionarySearch() {
            const q = (this.nciDictionaryQuery || '').trim();
            if (!q) {
                this.nciDictionaryError = '请输入要查询的术语。';
                return;
            }
            this.nciDictionaryLoading = true;
            this.nciDictionaryError = '';
            this.nciDictionarySearched = true;
            const encoded = encodeURIComponent(q);
            const match = this.nciDictionaryMatchType === 'Contains' ? 'Contains' : 'Begins';
            const url = `https://webapis.cancer.gov/glossary/v1/Terms/search/Cancer.gov/Patient/en/${encoded}?matchType=${match}&from=0&size=50`;
            try {
                const res = await fetch(url);
                if (!res.ok) {
                    throw new Error(`HTTP ${res.status}`);
                }
                const data = await res.json();
                this.nciDictionaryTotal = (data.meta && data.meta.totalResults) || 0;
                this.nciDictionaryResults = Array.isArray(data.results) ? data.results : [];
            } catch (e) {
                console.error('NCI dictionary search failed', e);
                this.nciDictionaryResults = [];
                this.nciDictionaryTotal = 0;
                this.nciDictionaryError = '查询失败，请检查网络或稍后重试。（术语以英文为主）';
            } finally {
                this.nciDictionaryLoading = false;
            }
        },

        async handleOptimize() {
            const text = this.userInput.trim();
            if (!text || this.isOptimizing) return;
            this.isOptimizing = true;
            try {
                const res = await fetch('/chat/optimize', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message: text })
                });
                if (res.ok) {
                    const data = await res.json();
                    if (data.questions && data.questions.length > 0) {
                        this.optimizedQuestions = data.questions;
                    } else {
                        alert('未能生成优化提问，请尝试修改输入。');
                    }
                } else {
                    alert('优化提问请求失败，请稍后再试。');
                }
            } catch (e) {
                console.error(e);
            } finally {
                this.isOptimizing = false;
            }
        },

        applyOptimizedQuestion(q) {
            this.userInput = q;
            this.optimizedQuestions = [];
            this.$nextTick(() => {
                if (this.$refs.textarea) {
                    this.$refs.textarea.focus();
                    this.autoResize({ target: this.$refs.textarea });
                }
            });
        },

        escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        },
        
        handleCompositionStart() {
            this.isComposing = true;
        },
        
        handleCompositionEnd() {
            this.isComposing = false;
        },
        
        handleKeyDown(event) {
            // 如果是回车键且不是Shift+回车，且不在输入法组合中
            if (event.key === 'Enter' && !event.shiftKey && !this.isComposing) {
                event.preventDefault();
                this.handleSend();
            }
        },
        
        handleStop() {
            if (this.abortController) {
                this.abortController.abort();
            }
        },
        
        async handleSend() {
            const text = this.userInput.trim();
            if (!text || this.isLoading || this.isComposing) return;
            
            this.optimizedQuestions = []; // 发送前清空优化建议
            
            // Add user message
            this.messages.push({
                text: text,
                isUser: true,
                isComplete: true
            });
            
            // 立即将新对话添加到侧边栏，形成秒出卡片的体验
            if (this.messages.length === 1) {
                const tempTitle = text.length > 10 ? text.substring(0, 10) + '...' : text;
                const existingSession = this.sessions.find(s => s.session_id === this.sessionId);
                if (!existingSession) {
                    this.sessions.unshift({
                        session_id: this.sessionId,
                        title: tempTitle,
                        message_count: 1,
                        updated_at: new Date().toISOString()
                    });
                }
            }
            
            this.userInput = '';
            this.$nextTick(() => {
                this.resetTextareaHeight();
                this.scrollToBottom();
            });

            // Show loading
            this.isLoading = true;

            // 立刻创建气泡，显示思考动画（二合一：思考 + 流式输出在同一个气泡）
            this.messages.push({ 
                text: '', 
                isUser: false, 
                isThinking: true,
                isComplete: false,
                ragTrace: null,
                ragSteps: [],
                followUps: []
            });
            const botMsgIdx = this.messages.length - 1;

            // 用于终止请求
            this.abortController = new AbortController();

            try {
                const response = await fetch('/chat/stream', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ 
                        message: text,
                        user_id: this.userId,
                        session_id: this.sessionId,
                        think_mode: this.thinkMode
                    }),
                    signal: this.abortController.signal,
                });

                if (!response.ok) throw new Error(`HTTP ${response.status}`);

                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                
                let buffer = '';
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    
                    buffer += decoder.decode(value, { stream: true });
                    
                    let eventEndIndex;
                    while ((eventEndIndex = buffer.indexOf('\n\n')) !== -1) {
                        const eventStr = buffer.slice(0, eventEndIndex);
                        buffer = buffer.slice(eventEndIndex + 2);
                        
                        if (eventStr.startsWith('data: ')) {
                            const dataStr = eventStr.slice(6);
                            if (dataStr === '[DONE]') continue;
                            try {
                                const data = JSON.parse(dataStr);
                                if (data.type === 'content') {
                                    // 收到第一个 token 时关闭思考动画
                                    if (this.messages[botMsgIdx].isThinking) {
                                        this.messages[botMsgIdx].isThinking = false;
                                    }
                                    this.messages[botMsgIdx].text += data.content;
                                } else if (data.type === 'trace') {
                                    this.messages[botMsgIdx].ragTrace = data.rag_trace;
                                } else if (data.type === 'rag_step') {
                                    // 实时 RAG 检索步骤 — 直接显示在思考气泡内
                                    if (!this.messages[botMsgIdx].ragSteps) {
                                        this.messages[botMsgIdx].ragSteps = [];
                                    }
                                    this.messages[botMsgIdx].ragSteps.push(data.step);
                                } else if (data.type === 'follow_ups') {
                                    this.messages[botMsgIdx].followUps = data.questions;
                                } else if (data.type === 'session_title') {
                                    // 乐观更新侧边栏标题（因为此时后端可能还没写入文件，直接请求会导致拉取不到数据）
                                    const s = this.sessions.find(s => s.session_id === data.session_id);
                                    if (s) {
                                        s.title = data.title;
                                        s.updated_at = new Date().toISOString();
                                        s.message_count = this.messages.length;
                                    } else {
                                        // 直接前置插入到本地历史记录数组中，实现“秒出”
                                        this.sessions.unshift({
                                            session_id: data.session_id,
                                            title: data.title,
                                            message_count: this.messages.length,
                                            updated_at: new Date().toISOString()
                                        });
                                    }
                                } else if (data.type === 'error') {
                                    this.messages[botMsgIdx].isThinking = false;
                                    this.messages[botMsgIdx].text += `\n[Error: ${data.content}]`;
                                }
                            } catch (e) {
                                console.warn('SSE parse error:', e);
                            }
                        }
                    }
                    this.$nextTick(() => this.scrollToBottom());
                }

            } catch (error) {
                if (error.name === 'AbortError') {
                    // 用户主动终止
                    this.messages[botMsgIdx].isThinking = false;
                    if (!this.messages[botMsgIdx].text) {
                        this.messages[botMsgIdx].text = '(已终止回答)';
                    } else {
                        this.messages[botMsgIdx].text += '\n\n_(回答已被终止)_';
                    }
                } else {
                    console.error('Error:', error);
                    this.messages[botMsgIdx].isThinking = false;
                    this.messages[botMsgIdx].text = `喵呜... 出了点问题：${error.message}`;
                }
            } finally {
                this.isLoading = false;
                this.abortController = null;
                if (this.messages[botMsgIdx]) {
                    this.messages[botMsgIdx].isComplete = true;
                }
                this.$nextTick(() => this.scrollToBottom());
            }
        },
        
        autoResize(event) {
            const textarea = event.target;
            textarea.style.height = 'auto';
            textarea.style.height = textarea.scrollHeight + 'px';
        },
        
        resetTextareaHeight() {
            if (this.$refs.textarea) {
                this.$refs.textarea.style.height = 'auto';
            }
        },
        
        scrollToBottom() {
            if (this.$refs.chatContainer) {
                this.$refs.chatContainer.scrollTop = this.$refs.chatContainer.scrollHeight;
            }
        },
        
        handleNewChat() {
            this.messages = [];
            this.sessionId = 'session_' + Date.now();
            this.activeNav = 'newChat';
            this.showHistorySidebar = false;
        },
        
        handleClearChat() {
            if (confirm('确定要清空当前对话吗？喵？')) {
                this.messages = [];
            }
        },
        
        async handleHistory() {
            this.activeNav = 'history';
            this.showHistorySidebar = true;
            try {
                const response = await fetch(`/sessions/${this.userId}`);
                if (!response.ok) {
                    throw new Error('Failed to load sessions');
                }
                const data = await response.json();
                this.sessions = data.sessions;
            } catch (error) {
                console.error('Error loading sessions:', error);
                alert('加载历史记录失败：' + error.message);
            }
        },
        
        async loadSession(sessionId) {
            this.sessionId = sessionId;
            this.showHistorySidebar = false;
            this.activeNav = 'newChat';
            
            // 从后端加载历史消息
            try {
                const response = await fetch(`/sessions/${this.userId}/${sessionId}`);
                if (!response.ok) {
                    throw new Error('Failed to load session messages');
                }
                const data = await response.json();
                
                // 转换消息格式并显示
                this.messages = data.messages.map(msg => ({
                    text: msg.content,
                    isUser: msg.type === 'human',
                    ragTrace: msg.rag_trace || null,
                    isComplete: true
                }));
                
                this.$nextTick(() => {
                    this.scrollToBottom();
                });
            } catch (error) {
                console.error('Error loading session:', error);
                alert('加载会话失败：' + error.message);
                this.messages = [];
            }
        },

        async deleteSession(sessionId) {
            if (!confirm(`确定要删除会话 "${sessionId}" 吗？`)) {
                return;
            }

            try {
                const response = await fetch(`/sessions/${this.userId}/${sessionId}`, {
                    method: 'DELETE'
                });

                const payload = await response.json().catch(() => ({}));
                if (!response.ok) {
                    throw new Error(payload.detail || 'Delete failed');
                }

                this.sessions = this.sessions.filter(s => s.session_id !== sessionId);

                if (this.sessionId === sessionId) {
                    this.messages = [];
                    this.sessionId = 'session_' + Date.now();
                    this.activeNav = 'newChat';
                }

                if (payload.message) {
                    alert(payload.message);
                }
            } catch (error) {
                console.error('Error deleting session:', error);
                alert('删除会话失败：' + error.message);
            }
        },
        
        handleSettings() {
            this.activeNav = 'settings';
            this.showHistorySidebar = false;
            // 加载文档列表
            this.loadDocuments();
        },
        
        handleProfile() {
            this.activeNav = 'profile';
            this.showHistorySidebar = false;
            this.loadProfile();
        },

        async loadProfile() {
            try {
                const response = await fetch(`/profile/${this.userId}`);
                if (response.ok) {
                    const data = await response.json();
                    this.userProfile = data.profile;
                }
            } catch (error) {
                console.error("加载个人档案失败", error);
            }
        },

        handleProfileSelect(event) {
            const files = event.target.files;
            if (files && files.length > 0) {
                this.selectedProfileFile = files[0];
                this.profileProgress = '';
            }
        },

        triggerProfileUpload(mode) {
            this.uploadMode = mode;
            if (this.$refs.profileInput) {
                this.$refs.profileInput.click();
            }
        },

        async uploadProfile() {
            if (!this.selectedProfileFile) {
                alert('请先选择病历文件');
                return;
            }
            
            this.isUploadingProfile = true;
            this.profileProgress = '正在使用多模态大模型解析病历资料，请稍候...';
            
            try {
                const formData = new FormData();
                formData.append('file', this.selectedProfileFile);
                formData.append('user_id', this.userId);
                formData.append('is_update', this.uploadMode === 'update' ? 'true' : 'false');
                
                const response = await fetch('/profile/upload', {
                    method: 'POST',
                    body: formData
                });
                
                if (!response.ok) {
                    const error = await response.json();
                    throw new Error(error.detail || 'Upload failed');
                }
                
                const data = await response.json();
                this.profileProgress = data.message;
                this.userProfile = data.profile;
                
                // 清空选择
                this.selectedProfileFile = null;
                if (this.$refs.profileInput) {
                    this.$refs.profileInput.value = '';
                }
                
            } catch (error) {
                console.error('Error uploading profile:', error);
                this.profileProgress = '解析失败：' + error.message;
            } finally {
                this.isUploadingProfile = false;
            }
        },

        sendProfileFollowUp(question) {
            this.activeNav = 'newChat';
            this.userInput = question;
            this.$nextTick(() => {
                this.handleSend();
            });
        },

        renderChart() {
            if (!this.selectedChartIndicator || !this.userProfile || !this.userProfile.test_items) return;
            
            const dataPoints = this.userProfile.test_items.filter(item => item.item_name === this.selectedChartIndicator);
            
            // Sort by date (assuming YYYY-MM-DD format)
            dataPoints.sort((a, b) => new Date(a.record_date || 0) - new Date(b.record_date || 0));
            
            const labels = dataPoints.map(item => item.record_date || '未知时间');
            const values = dataPoints.map(item => parseFloat(item.result) || 0);

            this.$nextTick(() => {
                const ctx = document.getElementById('indicatorChart');
                if (!ctx) return;
                
                if (this.chartInstance) {
                    this.chartInstance.destroy();
                }
                
                this.chartInstance = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: labels,
                        datasets: [{
                            label: this.selectedChartIndicator,
                            data: values,
                            borderColor: '#2ecc71', // 绿色折线
                            backgroundColor: 'rgba(46, 204, 113, 0.1)',
                            borderWidth: 2,
                            pointBackgroundColor: '#27ae60', // 较深的绿点
                            pointBorderColor: '#fff',
                            pointBorderWidth: 2,
                            pointRadius: 5,
                            pointHoverRadius: 7,
                            fill: true,
                            tension: 0.3
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: {
                                display: false // 隐藏顶部的冗余图例（Data-ink ratio）
                            },
                            tooltip: {
                                backgroundColor: 'rgba(255,255,255,0.9)',
                                titleColor: '#333',
                                bodyColor: '#666',
                                borderColor: '#eee',
                                borderWidth: 1,
                                padding: 10,
                                displayColors: false
                            }
                        },
                        scales: {
                            x: {
                                grid: {
                                    display: false, // 隐藏 X 轴网格线
                                    drawBorder: false
                                },
                                ticks: {
                                    font: { family: "'Nunito', sans-serif" },
                                    color: '#888'
                                }
                            },
                            y: {
                                beginAtZero: false,
                                grid: {
                                    display: false, // 隐藏 Y 轴网格线
                                    drawBorder: false
                                },
                                ticks: {
                                    font: { family: "'Nunito', sans-serif" },
                                    color: '#888'
                                }
                            }
                        }
                    }
                });
            });
        },

        async loadDocuments() {
            this.documentsLoading = true;
            try {
                const response = await fetch('/documents');
                if (!response.ok) {
                    throw new Error('Failed to load documents');
                }
                const data = await response.json();
                this.documents = data.documents;
            } catch (error) {
                console.error('Error loading documents:', error);
                alert('加载文档列表失败：' + error.message);
            } finally {
                this.documentsLoading = false;
            }
        },
        
        handleFileSelect(event) {
            const files = event.target.files;
            if (files && files.length > 0) {
                this.selectedFile = files[0];
                this.uploadProgress = '';
            }
        },
        
        async uploadDocument() {
            if (!this.selectedFile) {
                alert('请先选择文件');
                return;
            }
            
            this.isUploading = true;
            this.uploadProgress = '正在上传...';
            
            try {
                const formData = new FormData();
                formData.append('file', this.selectedFile);
                
                const response = await fetch('/documents/upload', {
                    method: 'POST',
                    body: formData
                });
                
                if (!response.ok) {
                    const error = await response.json();
                    throw new Error(error.detail || 'Upload failed');
                }
                
                const data = await response.json();
                this.uploadProgress = data.message;
                
                // 清空选择
                this.selectedFile = null;
                if (this.$refs.fileInput) {
                    this.$refs.fileInput.value = '';
                }
                
                // 刷新文档列表
                await this.loadDocuments();
                
                // 3秒后清除提示
                setTimeout(() => {
                    this.uploadProgress = '';
                }, 3000);
                
            } catch (error) {
                console.error('Error uploading document:', error);
                this.uploadProgress = '上传失败：' + error.message;
            } finally {
                this.isUploading = false;
            }
        },
        
        async deleteDocument(filename) {
            if (!confirm(`确定要删除文档 "${filename}" 吗？这将同时删除 Milvus 中的所有相关向量。`)) {
                return;
            }
            
            try {
                const response = await fetch(`/documents/${encodeURIComponent(filename)}`, {
                    method: 'DELETE'
                });
                
                if (!response.ok) {
                    const error = await response.json();
                    throw new Error(error.detail || 'Delete failed');
                }
                
                const data = await response.json();
                alert(data.message);
                
                // 刷新文档列表
                await this.loadDocuments();
                
            } catch (error) {
                console.error('Error deleting document:', error);
                alert('删除文档失败：' + error.message);
            }
        },
        
        getFileIcon(fileType) {
            if (fileType === 'PDF') {
                return 'fas fa-file-pdf';
            } else if (fileType === 'Word') {
                return 'fas fa-file-word';
            } else if (fileType === 'Excel') {
                return 'fas fa-file-excel';
            }
            return 'fas fa-file';
        }
    },
        watch: {
        showChartModal(newVal) {
            if (newVal) {
                if (!this.selectedChartIndicator && this.uniqueTestItemNames.length > 0) {
                    this.selectedChartIndicator = this.uniqueTestItemNames[0];
                }
                this.$nextTick(() => {
                    this.renderChart();
                });
            }
        },
        messages: {
            handler() {
                this.$nextTick(() => {
                    this.scrollToBottom();
                });
            },
            deep: true
        }
    }
}).mount('#app');
