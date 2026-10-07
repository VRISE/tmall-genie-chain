// 两只小狗「话题」实体补值：把「你可以帮我干什么 / 动画片」这一类说法灌进去。
// 在已登录的 https://iap.aligenie.com/console/newskill/113665/entity/101851 页面按 F12，
// Console 里整段粘贴回车。跑完点右上角「语音交互模型训练」。
(async () => {
  const SID = 113665, EID = 101851;
  const tok = (document.cookie.match(/ctoken=([^;&]+)/) || [])[1];
  const H = { "Content-Type": "application/json", "x-csrf-token": tok };
  const U = "/api/skills/" + SID + "/entities/" + EID + "/entries?ctoken=" + tok;
  const V = [
    ["你可以帮我干什么", "你能干什么|你会干什么|你能做什么|你会做什么|你能帮我什么|你有什么功能|你会什么|你能帮什么忙|帮助|你能干啥|你会啥|你有什么本事"],
    ["动画片", "动画|卡通|卡通片|动漫|少儿动画|看动画片|放动画片|能放动画片吗"],
    ["讲故事", "听故事|讲个故事|说故事|故事|讲一个故事"],
    ["听歌", "唱歌|放首歌|听首歌|音乐|放音乐"],
  ];
  for (const [v, syn] of V) {
    const synonyms = syn.split("|");
    let r = await fetch(U, {
      method: "POST", credentials: "include", headers: H,
      body: JSON.stringify({ normValue: v, synonyms }),
    });
    let j = await r.text();
    // 有的版本 POST 不吃 synonyms，那就再 PUT 一次补上
    const id = (j.match(/"id"\s*:\s*(\d+)/) || [])[1];
    if (id) {
      await fetch(U, {
        method: "PUT", credentials: "include", headers: H,
        body: JSON.stringify({ id: +id, entityId: EID, normValue: v, synonyms }),
      });
    }
    console.log(v, r.status, id || j.slice(0, 80));
  }
  console.log("灌完，去点「语音交互模型训练」");
})();
