<?php

declare(strict_types=1);

namespace App\Example;

use App\Models\User;

class CleanExample
{
    public function goodMethod(): array
    {
        return ['users' => User::query()->get()];
    }
}
