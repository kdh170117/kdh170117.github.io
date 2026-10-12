---
layout: page
title: 태그
permalink: /tags/
---
{%- assign tags = site.tags | sort -%}
<p class="tag-cloud">
{%- for t in tags %}<a class="tag" href="#{{ t[0] | slugify }}">#{{ t[0] }} <small>{{ t[1].size }}</small></a> {% endfor -%}
</p>
{%- for t in tags %}
<h3 id="{{ t[0] | slugify }}">#{{ t[0] }}</h3>
<ul>
{%- for post in t[1] %}
  <li><span class="post-meta">{{ post.date | date: "%Y-%m-%d %H:%M" }}</span> · <a href="{{ post.url | relative_url }}">{{ post.title | escape }}</a></li>
{%- endfor %}
</ul>
{%- endfor %}
<style>
.tag-cloud .tag{display:inline-block;margin:0 .4em .5em 0;padding:.15em .6em;border-radius:999px;background:#f0f0f0;color:#555;text-decoration:none;font-size:.9rem}
.tag-cloud small{color:#999}
</style>
